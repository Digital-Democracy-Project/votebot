"""Bill version sync service — detects newer bill text versions and re-ingests.

Replaces the daily bill-history/bill-votes sync with a targeted check:
1. For each current-session bill, fetch OpenStates `versions` array
2. Compare latest version against Redis cache
3. If newer: download bill text (PDF or HTML), re-ingest into Pinecone,
   update Webflow CMS gov-url
4. If unchanged: update last_checked timestamp only
"""

import asyncio
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
            # Update last_checked timestamp
            if cached:
                cached["last_checked"] = datetime.utcnow().isoformat()
                await redis_store.set_bill_version(webflow_id, cached)

            return VersionCheckResult(
                webflow_id=webflow_id,
                bill_title=bill_title,
                jurisdiction=jurisdiction_code,
                status="unchanged",
                version_note=latest_version.get("note", ""),
                version_date=latest_version.get("date", ""),
                text_url=text_url,
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

        # 7. Update Webflow gov-url if enabled
        webflow_updated = False
        skip_webflow = self._config.get("skip_webflow_update", False)
        if not skip_webflow:
            try:
                from votebot.services.webflow_lookup import WebflowLookupService

                lookup = WebflowLookupService(self.settings)
                webflow_updated = await lookup.update_bill_gov_url(webflow_id, text_url)
            except Exception as e:
                logger.warning(
                    "Failed to update Webflow gov-url (bill text still ingested)",
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
    ) -> VersionSyncBatchResult:
        """Batch entry point: check all current-session bills for version updates.

        Filters to current-session bills, applies rate limiting between
        OpenStates API calls, and respects max_updates_per_run config.

        Args:
            bills: List of bill dicts from Webflow CMS (raw items with fieldData)

        Returns:
            VersionSyncBatchResult with aggregate stats
        """
        from votebot.services.redis_store import get_redis_store
        from votebot.updates.bill_sync import BillSyncService

        sync_service = BillSyncService(self.settings)
        max_updates = self._config.get("max_updates_per_run", 50)

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
                continue

            # Check if current session
            if not sync_service.is_current_session(session_year, session_code, jurisdiction_code):
                result.skipped += 1
                continue

            # Check if we should sync this jurisdiction today
            if not sync_service.should_sync_jurisdiction(jurisdiction_code):
                result.skipped += 1
                continue

            # Respect max_updates_per_run
            if max_updates > 0 and updates_this_run >= max_updates:
                result.skipped += 1
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
                    updates_this_run += 1
                    logger.info(
                        "Bill version updated",
                        bill=title,
                        version=check_result.version_note,
                        date=check_result.version_date,
                        chunks=check_result.chunks_created,
                    )
                elif check_result.status == "unchanged":
                    result.unchanged += 1
                elif check_result.status == "no_versions":
                    result.no_versions += 1
                elif check_result.status == "error":
                    result.failed += 1
                    if check_result.error:
                        result.errors.append(f"{title}: {check_result.error}")

            except Exception as e:
                result.failed += 1
                result.errors.append(f"{title}: {e}")
                logger.error(
                    "Unexpected error checking bill version",
                    bill=title,
                    webflow_id=webflow_id,
                    error=str(e),
                )

        logger.info(
            "Bill version sync batch complete",
            total=result.total_bills,
            checked=result.checked,
            updated=result.updated,
            unchanged=result.unchanged,
            no_versions=result.no_versions,
            skipped=result.skipped,
            failed=result.failed,
            chunks_created=result.chunks_created,
            webflow_updates=result.webflow_updates,
        )

        return result
