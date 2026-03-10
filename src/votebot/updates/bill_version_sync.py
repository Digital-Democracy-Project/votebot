"""Bill version sync service — detects newer bill text versions and re-ingests.

Replaces the daily bill-history/bill-votes sync with a targeted check:
1. For each current-session bill, fetch OpenStates `versions` array
2. Compare latest version against Redis cache
3. If newer: download bill text (PDF or HTML), re-ingest into Pinecone,
   update Webflow CMS gov-url
4. If unchanged: update last_checked timestamp only
"""

import asyncio
import gc
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import structlog
import yaml

from votebot.config import Settings, get_settings

logger = structlog.get_logger()

DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "config" / "sync_schedule.yaml"


@dataclass
class VersionCheckResult:
    """Result of checking a single bill's version."""

    webflow_id: str
    bill_title: str
    jurisdiction: str
    status: str  # "updated", "unchanged", "no_versions", "error", "skipped"
    version_note: str = ""
    version_date: str = ""
    text_url: str = ""
    chunks_created: int = 0
    webflow_updated: bool = False
    status_updated: bool = False
    webflow_patch_skipped: bool = False
    error: str | None = None


@dataclass
class VersionSyncBatchResult:
    """Aggregate result of a batch version sync run."""

    total_bills: int = 0
    checked: int = 0
    updated: int = 0
    unchanged: int = 0
    no_versions: int = 0
    skipped: int = 0
    failed: int = 0
    chunks_created: int = 0
    webflow_updates: int = 0
    status_updates: int = 0
    webflow_skipped: int = 0
    webflow_patch_failures: int = 0
    no_latest_action: int = 0
    skipped_no_url: int = 0
    skipped_not_current: int = 0
    skipped_jurisdiction: int = 0
    errors: list[str] = field(default_factory=list)


class BillVersionSyncService:
    """Service for detecting and syncing newer bill text versions.

    Delegates to existing services:
    - BillSyncService: OpenStates URL parsing, API fetching, session detection, rate limiting
    - WebflowSource: PDF/HTML text extraction and document creation
    - IngestionPipeline: Chunking and Pinecone upsert
    - WebflowLookupService: CMS gov-url update
    - RedisStore: Version cache
    """

    def __init__(self, settings: Settings | None = None, config_path: Path | None = None):
        self.settings = settings or get_settings()
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self._config = self._load_config()

    def _load_config(self) -> dict[str, Any]:
        """Load bill_version_check config from sync_schedule.yaml."""
        if not self.config_path.exists():
            return {}
        try:
            with open(self.config_path) as f:
                config = yaml.safe_load(f) or {}
            return config.get("bill_version_check", {})
        except Exception as e:
            logger.error("Failed to load bill version sync config", error=str(e))
            return {}

    @staticmethod
    def _get_latest_version(versions: list[dict]) -> dict | None:
        """Get the latest version from OpenStates versions array.

        Sorts by date descending, returns the first entry.
        OpenStates versions have: date, note, links[{url, media_type}].
        """
        if not versions:
            return None
        # Sort by date descending — versions without dates go last
        sorted_versions = sorted(
            versions,
            key=lambda v: v.get("date") or "",
            reverse=True,
        )
        return sorted_versions[0]

    @staticmethod
    def _get_best_text_url(version: dict) -> tuple[str, str] | None:
        """Extract best URL + media_type from a version's links.

        Priority: application/pdf first, then text/html.

        Returns:
            (url, media_type) tuple, or None if no usable links
        """
        links = version.get("links", [])
        if not links:
            return None

        # Prefer PDF over HTML
        pdf_link = None
        html_link = None

        for link in links:
            url = link.get("url", "")
            media_type = (link.get("media_type") or "").lower()

            if not url:
                continue

            if "application/pdf" in media_type:
                pdf_link = (url, "application/pdf")
            elif "text/html" in media_type:
                html_link = (url, "text/html")
            elif not pdf_link and not html_link:
                # Unknown media type — keep as fallback
                html_link = (url, media_type or "unknown")

        return pdf_link or html_link

    @staticmethod
    def _is_newer_version(latest_version: dict, cached: dict | None) -> bool:
        """Determine if the latest version is newer than cached.

        Returns True if:
        - No cache exists (first run)
        - Date is newer
        - Same date but different version note (e.g., "Engrossed" vs "Introduced")
        - URL changed
        """
        if cached is None:
            return True

        latest_date = latest_version.get("date") or ""
        cached_date = cached.get("version_date") or ""

        # Newer date
        if latest_date > cached_date:
            return True

        # Same date, different note
        latest_note = latest_version.get("note") or ""
        cached_note = cached.get("version_note") or ""
        if latest_date == cached_date and latest_note != cached_note:
            return True

        # URL changed
        best_url = BillVersionSyncService._get_best_text_url(latest_version)
        if best_url:
            url, _ = best_url
            if url != cached.get("text_url", ""):
                return True

        return False

    @staticmethod
    def _extract_latest_action(bill_data: dict) -> tuple[str | None, str | None]:
        """Extract the latest action description and date from OpenStates bill data.

        OpenStates v3 API returns `latest_action_description` and
        `latest_action_date` as top-level fields. The date is YYYY-MM-DD;
        we convert it to ISO 8601 for Webflow's timestamp field type.

        Returns:
            (description, iso_date) tuple — either may be None
        """
        description = bill_data.get("latest_action_description") or None
        action_date = bill_data.get("latest_action_date")
        # Convert to Webflow-compatible ISO 8601 timestamp.
        # OpenStates returns either "YYYY-MM-DD" or full ISO like
        # "2026-02-25T17:37:53+00:00" — only append time suffix if
        # the date doesn't already contain one.
        if action_date and "T" not in action_date:
            iso_date = f"{action_date}T00:00:00.000Z"
        elif action_date:
            # Already has time component; normalize to Webflow format
            from datetime import datetime, timezone
            try:
                dt = datetime.fromisoformat(action_date)
                iso_date = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            except (ValueError, TypeError):
                iso_date = action_date
        else:
            iso_date = None
        return description, iso_date

    @staticmethod
    def _dates_match(cms_date: str | None, openstates_date: str | None) -> bool:
        """Compare a Webflow CMS date with an OpenStates-derived date.

        Webflow may return dates with varying precision (e.g.
        ``2026-02-25T00:00:00.000Z`` vs ``2026-02-25T00:00:00Z``).
        We normalise both to ``YYYY-MM-DD`` before comparing.
        """
        if not cms_date and not openstates_date:
            return True
        if not cms_date or not openstates_date:
            return False
        return cms_date[:10] == openstates_date[:10]

    async def _update_webflow_status(
        self,
        webflow_id: str,
        bill_title: str,
        new_status: str,
        status_date: str | None = None,
    ) -> bool:
        """Update the status and status-date fields for a bill in Webflow CMS.

        Args:
            webflow_id: Webflow item ID
            bill_title: For logging
            new_status: Latest action description from OpenStates
            status_date: ISO 8601 timestamp for the action date

        Returns:
            True on success, False on failure
        """
        try:
            from votebot.services.webflow_lookup import WebflowLookupService

            lookup = WebflowLookupService(self.settings)
            scheduler_key = self.settings.webflow_scheduler_api_key.get_secret_value()
            field_data: dict[str, str] = {"status": new_status}
            if status_date:
                field_data["status-date"] = status_date
            return await lookup.update_bill_fields(
                webflow_id,
                field_data,
                api_key=scheduler_key or None,
            )
        except Exception as e:
            logger.warning(
                "Failed to update Webflow status",
                webflow_id=webflow_id,
                bill_title=bill_title,
                error=str(e),
            )
            return False

    async def check_and_update_bill(
        self,
        webflow_id: str,
        bill_title: str,
        jurisdiction_code: str,
        openstates_url: str,
        bill_slug: str,
        fields: dict,
    ) -> VersionCheckResult:
        """Check a single bill for version updates and re-ingest if newer.

        Args:
            webflow_id: Webflow item ID
            bill_title: Human-readable bill title
            jurisdiction_code: Two-letter state code
            openstates_url: OpenStates URL for the bill
            bill_slug: Webflow slug for DDP linking
            fields: Full CMS field data from Webflow

        Returns:
            VersionCheckResult with outcome details
        """
        from votebot.services.redis_store import get_redis_store
        from votebot.updates.bill_sync import BillSyncService

        sync_service = BillSyncService(self.settings)

        # 1. Parse OpenStates URL
        parsed = sync_service.parse_openstates_url(openstates_url)
        if not parsed:
            return VersionCheckResult(
                webflow_id=webflow_id,
                bill_title=bill_title,
                jurisdiction=jurisdiction_code,
                status="error",
                error=f"Could not parse OpenStates URL: {openstates_url}",
            )

        # 2. Fetch bill from OpenStates (reuses retry/rate-limit logic)
        bill_data = await sync_service.fetch_bill_from_openstates(
            parsed.jurisdiction, parsed.session, parsed.bill_id
        )
        if not bill_data:
            return VersionCheckResult(
                webflow_id=webflow_id,
                bill_title=bill_title,
                jurisdiction=jurisdiction_code,
                status="error",
                error=f"Failed to fetch bill from OpenStates: {openstates_url}",
            )

        # 3. Get latest version
        versions = bill_data.get("versions", [])
        latest_version = self._get_latest_version(versions)
        if not latest_version:
            return VersionCheckResult(
                webflow_id=webflow_id,
                bill_title=bill_title,
                jurisdiction=jurisdiction_code,
                status="no_versions",
            )

        # 4. Get best text URL from the version's links
        url_info = self._get_best_text_url(latest_version)
        if not url_info:
            return VersionCheckResult(
                webflow_id=webflow_id,
                bill_title=bill_title,
                jurisdiction=jurisdiction_code,
                status="no_versions",
                version_note=latest_version.get("note", ""),
                version_date=latest_version.get("date", ""),
            )

        text_url, media_type = url_info

        # 5. Compare against Redis cache
        redis_store = get_redis_store()
        cached = await redis_store.get_bill_version(webflow_id)

        if not self._is_newer_version(latest_version, cached):
            # Text version unchanged — always update status/status-date
            # in Webflow CMS so the CMS stays current even when bill text
            # hasn't changed (e.g., committee vote, floor action).
            latest_action, action_date = self._extract_latest_action(bill_data)
            status_updated = False
            patch_skipped = False

            if latest_action:
                skip_webflow = self._config.get("skip_webflow_update", False)
                cms_status = fields.get("status", "")
                cms_date = fields.get("status-date", "")
                status_matches = (
                    cms_status == latest_action
                    and self._dates_match(cms_date, action_date)
                )
                if status_matches:
                    patch_skipped = True
                    logger.debug(
                        "Skipping Webflow PATCH — status already matches",
                        bill_title=bill_title,
                        status=latest_action,
                    )
                elif not skip_webflow:
                    status_updated = await self._update_webflow_status(
                        webflow_id, bill_title, latest_action, action_date,
                    )
                    if status_updated:
                        logger.info(
                            "Bill status updated (version unchanged)",
                            bill_title=bill_title,
                            new_status=latest_action,
                            status_date=action_date,
                        )
                    else:
                        logger.warning(
                            "Webflow status PATCH failed (version unchanged path)",
                            bill_title=bill_title,
                            webflow_id=webflow_id,
                            attempted_status=latest_action,
                            attempted_date=action_date,
                            cms_status=cms_status,
                            cms_date=cms_date,
                        )

            else:
                logger.warning(
                    "No latest_action from OpenStates — status not updated",
                    bill_title=bill_title,
                    webflow_id=webflow_id,
                    jurisdiction=jurisdiction_code,
                    openstates_url=openstates_url,
                    cms_status=fields.get("status", ""),
                )

            # Update last_checked and last_status in cache
            if cached:
                cached["last_checked"] = datetime.utcnow().isoformat()
                if latest_action:
                    cached["last_status"] = latest_action
                await redis_store.set_bill_version(webflow_id, cached)

            return VersionCheckResult(
                webflow_id=webflow_id,
                bill_title=bill_title,
                jurisdiction=jurisdiction_code,
                status="unchanged",
                version_note=latest_version.get("note", ""),
                version_date=latest_version.get("date", ""),
                text_url=text_url,
                status_updated=status_updated,
                webflow_patch_skipped=patch_skipped,
            )

        # 6. Newer version detected — re-ingest bill text
        logger.info(
            "New bill version detected",
            bill_title=bill_title,
            webflow_id=webflow_id,
            version_note=latest_version.get("note", ""),
            version_date=latest_version.get("date", ""),
            text_url=text_url,
            media_type=media_type,
        )

        chunks_created = 0
        try:
            chunks_created = await self._ingest_bill_text(
                webflow_id=webflow_id,
                bill_title=bill_title,
                bill_slug=bill_slug,
                text_url=text_url,
                media_type=media_type,
                fields=fields,
            )
        except Exception as e:
            logger.error(
                "Failed to ingest bill text",
                webflow_id=webflow_id,
                bill_title=bill_title,
                error=str(e),
            )
            return VersionCheckResult(
                webflow_id=webflow_id,
                bill_title=bill_title,
                jurisdiction=jurisdiction_code,
                status="error",
                version_note=latest_version.get("note", ""),
                version_date=latest_version.get("date", ""),
                text_url=text_url,
                error=f"Ingestion failed: {e}",
            )

        # 7. Update Webflow fields (gov-url + status + status-date) in a single PATCH
        webflow_updated = False
        status_updated = False
        patch_skipped = False
        latest_action, action_date = self._extract_latest_action(bill_data)
        if not latest_action:
            logger.warning(
                "No latest_action from OpenStates for new version — status will be empty",
                bill_title=bill_title,
                webflow_id=webflow_id,
                jurisdiction=jurisdiction_code,
            )
        skip_webflow = self._config.get("skip_webflow_update", False)
        if not skip_webflow:
            try:
                from votebot.services.webflow_lookup import WebflowLookupService

                lookup = WebflowLookupService(self.settings)
                scheduler_key = self.settings.webflow_scheduler_api_key.get_secret_value()

                # Batch gov-url, status, and status-date into a single PATCH call
                # Only include fields whose values actually differ from CMS
                field_data: dict[str, str] = {}
                if fields.get("gov-url") != text_url:
                    field_data["gov-url"] = text_url
                if latest_action and fields.get("status") != latest_action:
                    field_data["status"] = latest_action
                if action_date and not self._dates_match(fields.get("status-date"), action_date):
                    field_data["status-date"] = action_date

                if not field_data:
                    patch_skipped = True
                    logger.debug(
                        "Skipping Webflow PATCH — all fields already match",
                        bill_title=bill_title,
                    )
                else:
                    success = await lookup.update_bill_fields(
                        webflow_id,
                        field_data,
                        api_key=scheduler_key or None,
                    )
                    if success:
                        webflow_updated = True
                        if "status" in field_data:
                            status_updated = True
                    else:
                        logger.warning(
                            "Webflow PATCH failed (new version path)",
                            bill_title=bill_title,
                            webflow_id=webflow_id,
                            attempted_fields=list(field_data.keys()),
                            field_data=field_data,
                        )
            except Exception as e:
                logger.warning(
                    "Failed to update Webflow fields (bill text still ingested)",
                    webflow_id=webflow_id,
                    error=str(e),
                )

        # 8. Update Redis cache
        version_data = {
            "version_date": latest_version.get("date", ""),
            "version_note": latest_version.get("note", ""),
            "text_url": text_url,
            "media_type": media_type,
            "last_checked": datetime.utcnow().isoformat(),
            "last_status": latest_action or "",
        }
        await redis_store.set_bill_version(webflow_id, version_data)

        return VersionCheckResult(
            webflow_id=webflow_id,
            bill_title=bill_title,
            jurisdiction=jurisdiction_code,
            status="updated",
            version_note=latest_version.get("note", ""),
            version_date=latest_version.get("date", ""),
            text_url=text_url,
            chunks_created=chunks_created,
            webflow_updated=webflow_updated,
            status_updated=status_updated,
            webflow_patch_skipped=patch_skipped,
        )

    async def _ingest_bill_text(
        self,
        webflow_id: str,
        bill_title: str,
        bill_slug: str,
        text_url: str,
        media_type: str,
        fields: dict,
    ) -> int:
        """Download bill text and ingest into Pinecone.

        Routes based on media_type:
        - "application/pdf" → WebflowSource._process_bill_pdf()
        - "text/html" → WebflowSource._process_bill_html()
        - Unknown → detect via _get_url_content_type(), then route

        Returns:
            Number of chunks created
        """
        from votebot.ingestion.pipeline import IngestionPipeline
        from votebot.ingestion.sources.webflow import WebflowSource

        webflow_source = WebflowSource(self.settings)
        pipeline = IngestionPipeline(self.settings)

        # Determine content type
        if "pdf" in media_type.lower():
            doc = await webflow_source._process_bill_pdf(text_url, fields, webflow_id)
        elif "html" in media_type.lower():
            doc = await webflow_source._process_bill_html(text_url, fields, webflow_id)
        else:
            # Unknown media type — detect
            detected = await webflow_source._get_url_content_type(text_url)
            if detected == "pdf":
                doc = await webflow_source._process_bill_pdf(text_url, fields, webflow_id)
            elif detected == "html":
                doc = await webflow_source._process_bill_html(text_url, fields, webflow_id)
            else:
                logger.warning(
                    "Cannot determine content type for bill text URL",
                    url=text_url,
                    media_type=media_type,
                    detected=detected,
                )
                return 0

        if not doc:
            logger.warning(
                "No document produced from bill text extraction",
                webflow_id=webflow_id,
                url=text_url,
            )
            return 0

        # Ingest with skip_duplicates=False to force overwrite
        result = await pipeline.ingest_document(
            content=doc.content,
            metadata=doc.metadata,
            skip_duplicates=False,
        )

        logger.info(
            "Bill text re-ingested",
            webflow_id=webflow_id,
            bill_title=bill_title,
            chunks_created=result.chunks_created,
            chunks_upserted=result.chunks_upserted,
        )

        return result.chunks_created

    async def sync_bill_versions(
        self,
        bills: list[dict[str, Any]],
        heartbeat_callback: Any | None = None,
    ) -> VersionSyncBatchResult:
        """Batch entry point: check all current-session bills for version updates.

        Filters to current-session bills, applies rate limiting between
        OpenStates API calls. Optionally caps re-ingestions via max_updates_per_run
        config (0 = unlimited).

        Args:
            bills: List of bill dicts from Webflow CMS (raw items with fieldData)

        Returns:
            VersionSyncBatchResult with aggregate stats
        """
        from votebot.services.redis_store import get_redis_store
        from votebot.updates.bill_sync import BillSyncService

        sync_service = BillSyncService(self.settings)
        max_updates = self._config.get("max_updates_per_run", 0)
        skip_webflow = self._config.get("skip_webflow_update", False)

        logger.info(
            "Starting bill version sync batch",
            total_bills=len(bills),
            max_updates_per_run=max_updates,
            skip_webflow_update=skip_webflow,
        )

        result = VersionSyncBatchResult(total_bills=len(bills))

        # Warm legislative calendar with live OpenStates session data
        jurisdiction_codes = set()
        for bill in bills:
            fields = bill.get("fieldData", {})
            jurisdiction_id = fields.get("jurisdiction", "")
            openstates_url = fields.get("open-states-url-2", "")
            code = sync_service.resolve_jurisdiction_code(jurisdiction_id, openstates_url)
            if code:
                jurisdiction_codes.add(code)

        jurisdiction_data = {}
        for code in jurisdiction_codes:
            try:
                info = await sync_service.get_jurisdiction_info(code)
                if info:
                    jurisdiction_data[code] = info
            except Exception as e:
                logger.warning("Failed to fetch jurisdiction for calendar warm", state=code, error=str(e))

        if jurisdiction_data:
            sync_service.calendar.warm_cache(jurisdiction_data)

        # Track active jurisdictions in Redis
        if jurisdiction_codes:
            redis_store = get_redis_store()
            for code in jurisdiction_codes:
                await redis_store.add_active_jurisdiction(code)

        updates_this_run = 0

        for bill in bills:
            fields = bill.get("fieldData", {})
            webflow_id = bill.get("id", "")
            title = fields.get("name", "Unknown")
            openstates_url = fields.get("open-states-url-2", "")
            session_year = fields.get("session-year", "")
            session_code = fields.get("session-code", "")
            jurisdiction_id = fields.get("jurisdiction", "")
            slug = fields.get("slug", "")

            jurisdiction_code = sync_service.resolve_jurisdiction_code(
                jurisdiction_id, openstates_url
            )

            # Skip bills without OpenStates URL
            if not openstates_url:
                result.skipped += 1
                result.skipped_no_url += 1
                logger.info(
                    "Skipping bill (no OpenStates URL)",
                    bill=title,
                    webflow_id=webflow_id,
                    slug=slug,
                    cms_status=fields.get("status", ""),
                )
                continue

            # Check if current session
            if not sync_service.is_current_session(session_year, session_code, jurisdiction_code):
                result.skipped += 1
                result.skipped_not_current += 1
                logger.info(
                    "Skipping bill (not current session)",
                    bill=title,
                    webflow_id=webflow_id,
                    jurisdiction=jurisdiction_code,
                    session_year=session_year,
                    session_code=session_code,
                )
                continue

            # Check if we should sync this jurisdiction today
            if not sync_service.should_sync_jurisdiction(jurisdiction_code):
                result.skipped += 1
                result.skipped_jurisdiction += 1
                logger.info(
                    "Skipping bill (jurisdiction not scheduled today)",
                    bill=title,
                    jurisdiction=jurisdiction_code,
                )
                continue

            # Respect max_updates_per_run
            if max_updates > 0 and updates_this_run >= max_updates:
                result.skipped += 1
                logger.info("Skipping bill (max updates reached)", bill=title, max_updates=max_updates)
                continue

            # Apply rate limiting
            await sync_service._apply_rate_limit()

            # Check and update
            try:
                check_result = await self.check_and_update_bill(
                    webflow_id=webflow_id,
                    bill_title=title,
                    jurisdiction_code=jurisdiction_code,
                    openstates_url=openstates_url,
                    bill_slug=slug,
                    fields=fields,
                )

                result.checked += 1

                if check_result.status == "updated":
                    result.updated += 1
                    result.chunks_created += check_result.chunks_created
                    if check_result.webflow_updated:
                        result.webflow_updates += 1
                    if check_result.status_updated:
                        result.status_updates += 1
                    if check_result.webflow_patch_skipped:
                        result.webflow_skipped += 1
                    if not check_result.webflow_updated and not check_result.webflow_patch_skipped:
                        result.webflow_patch_failures += 1
                    updates_this_run += 1
                    logger.info(
                        "Bill version updated",
                        bill=title,
                        webflow_id=webflow_id,
                        version=check_result.version_note,
                        date=check_result.version_date,
                        chunks=check_result.chunks_created,
                        webflow_updated=check_result.webflow_updated,
                        status_updated=check_result.status_updated,
                    )
                elif check_result.status == "unchanged":
                    result.unchanged += 1
                    if check_result.status_updated:
                        result.status_updates += 1
                    if check_result.webflow_patch_skipped:
                        result.webflow_skipped += 1
                    # Detect silent failures: not updated AND not skipped = PATCH failed or no action
                    if not check_result.status_updated and not check_result.webflow_patch_skipped:
                        result.webflow_patch_failures += 1
                    logger.info(
                        "Bill version unchanged",
                        bill=title,
                        webflow_id=webflow_id,
                        version=check_result.version_note,
                        status_updated=check_result.status_updated,
                        patch_skipped=check_result.webflow_patch_skipped,
                    )
                elif check_result.status == "no_versions":
                    result.no_versions += 1
                    logger.warning(
                        "Bill has no versions in OpenStates",
                        bill=title,
                        webflow_id=webflow_id,
                    )
                elif check_result.status == "error":
                    result.failed += 1
                    if check_result.error:
                        result.errors.append(f"{title}: {check_result.error}")
                    logger.warning(
                        "Bill version check failed",
                        bill=title,
                        webflow_id=webflow_id,
                        error=check_result.error,
                    )

            except Exception as e:
                result.failed += 1
                result.errors.append(f"{title}: {e}")
                logger.error(
                    "Unexpected error checking bill version",
                    bill=title,
                    webflow_id=webflow_id,
                    error=str(e),
                )

            # Keep heartbeat alive during long-running version sync phase
            if heartbeat_callback and result.checked % 10 == 0:
                await heartbeat_callback()

            # Reclaim memory between bills (PDF objects, embedding vectors, etc.)
            gc.collect()

        logger.info(
            "Bill version sync batch complete",
            total=result.total_bills,
            checked=result.checked,
            updated=result.updated,
            unchanged=result.unchanged,
            no_versions=result.no_versions,
            skipped=result.skipped,
            skipped_no_url=result.skipped_no_url,
            skipped_not_current=result.skipped_not_current,
            skipped_jurisdiction=result.skipped_jurisdiction,
            failed=result.failed,
            chunks_created=result.chunks_created,
            webflow_updates=result.webflow_updates,
            status_updates=result.status_updates,
            webflow_skipped=result.webflow_skipped,
            webflow_patch_failures=result.webflow_patch_failures,
            no_latest_action=result.no_latest_action,
            errors=result.errors[:10] if result.errors else [],
        )

        return result
