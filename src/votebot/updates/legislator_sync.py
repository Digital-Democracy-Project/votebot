"""Legislator bills sync service for fetching sponsored bills from OpenStates."""

import asyncio
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import structlog
import yaml

from votebot.config import Settings, get_settings
from votebot.ingestion.metadata import DocumentMetadata
from votebot.ingestion.pipeline import IngestionPipeline

logger = structlog.get_logger()

# Default config path
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "config" / "sync_schedule.yaml"


@dataclass
class RateLimitConfig:
    """Rate limiting configuration."""

    requests_per_minute: int = 60
    delay_between_requests_ms: int = 100
    max_retry_attempts: int = 3
    retry_backoff_seconds: int = 5


@dataclass
class LegislatorSyncResult:
    """Result of syncing a single legislator's sponsored bills."""

    legislator_id: str
    legislator_name: str
    success: bool
    bills_found: int = 0
    chunks_created: int = 0
    error: str | None = None


@dataclass
class SyncBatchResult:
    """Result of syncing a batch of legislators."""

    total_legislators: int
    successful: int
    failed: int
    total_bills_found: int
    chunks_created: int
    errors: list[str]


class LegislatorSyncService:
    """
    Service for syncing legislator sponsored bills from OpenStates API v3.

    Creates separate `legislator-bills` documents to enable queries like
    "What bills has Rick Scott sponsored?" without overwriting
    Webflow-sourced legislator content.
    """

    OPENSTATES_API_BASE = "https://v3.openstates.org"

    def __init__(self, settings: Settings | None = None, config_path: Path | None = None):
        """
        Initialize the legislator sync service.

        Args:
            settings: Application settings
            config_path: Path to sync_schedule.yaml config file
        """
        self.settings = settings or get_settings()
        self.api_key = self.settings.openstates_api_key.get_secret_value()
        self.pipeline = IngestionPipeline(self.settings)
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self.rate_limit = self._load_rate_limit_config()
        self._last_request_time: float = 0
        # Cache for bill slug lookups (webflow_bill_id -> {name, slug, identifier})
        self._bill_cache: dict[str, dict] = {}

    def _load_rate_limit_config(self) -> RateLimitConfig:
        """Load rate limit configuration from YAML file."""
        if not self.config_path.exists():
            logger.warning(f"Sync config not found at {self.config_path}, using defaults")
            return RateLimitConfig()

        try:
            with open(self.config_path) as f:
                config = yaml.safe_load(f) or {}

            rate_limit = config.get("rate_limit", {})
            retry = config.get("retry", {})

            return RateLimitConfig(
                requests_per_minute=rate_limit.get("requests_per_minute", 60),
                delay_between_requests_ms=rate_limit.get("delay_between_bills_ms", 100),
                max_retry_attempts=retry.get("max_attempts", 3),
                retry_backoff_seconds=retry.get("backoff_seconds", 5),
            )
        except Exception as e:
            logger.error(f"Failed to load rate limit config: {e}")
            return RateLimitConfig()

    async def _build_bill_mapping(self) -> None:
        """
        Build a mapping from OpenStates bill IDs to Webflow bill info.

        Fetches the bills collection from Webflow and creates a lookup table
        for resolving bill references to DDP URLs.
        """
        if self._bill_cache:
            return  # Already cached

        bills_collection_id = self.settings.webflow_bills_collection_id
        if not bills_collection_id:
            logger.warning("No bills collection ID configured, skipping bill mapping")
            return

        webflow_api_key = self.settings.webflow_api_key.get_secret_value()
        if not webflow_api_key:
            logger.warning("No Webflow API key configured, skipping bill mapping")
            return

        logger.info("Building bill mapping for legislator-bills linking...")

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                headers = {
                    "Authorization": f"Bearer {webflow_api_key}",
                    "accept": "application/json",
                }

                offset = 0
                page_size = 100

                while True:
                    params = {"limit": page_size, "offset": offset}
                    response = await client.get(
                        f"https://api.webflow.com/v2/collections/{bills_collection_id}/items",
                        headers=headers,
                        params=params,
                    )
                    response.raise_for_status()
                    data = response.json()
                    items = data.get("items", [])

                    if not items:
                        break

                    for item in items:
                        fields = item.get("fieldData", {})
                        openstates_url = fields.get("open-states-url-2", "")
                        name = fields.get("name", "")
                        slug = fields.get("slug", "")
                        bill_prefix = fields.get("bill-prefix", "")
                        bill_number = fields.get("bill-number", "")
                        identifier = f"{bill_prefix} {bill_number}".strip() if bill_prefix else ""

                        # Extract OpenStates bill ID from URL if available
                        # URL format: https://openstates.org/{jurisdiction}/bills/{session}/{bill_id}/
                        if openstates_url and slug:
                            # Use the bill identifier as key for matching
                            self._bill_cache[identifier] = {
                                "name": name,
                                "slug": slug,
                                "identifier": identifier,
                            }

                    pagination = data.get("pagination", {})
                    total = pagination.get("total", 0)
                    if offset + len(items) >= total or len(items) < page_size:
                        break

                    offset += page_size

            logger.info(
                f"Built bill mapping with {len(self._bill_cache)} entries"
            )

        except Exception as e:
            logger.warning(
                "Failed to build bill mapping",
                error=str(e),
            )

    async def _apply_rate_limit(self) -> None:
        """Apply rate limiting by sleeping if needed."""
        import time

        # Calculate minimum delay between requests
        min_delay_seconds = self.rate_limit.delay_between_requests_ms / 1000.0

        # Also respect requests_per_minute limit
        per_request_delay = 60.0 / self.rate_limit.requests_per_minute
        min_delay_seconds = max(min_delay_seconds, per_request_delay)

        # Calculate time since last request
        current_time = time.time()
        elapsed = current_time - self._last_request_time

        # Sleep if we need to wait
        if elapsed < min_delay_seconds and self._last_request_time > 0:
            sleep_time = min_delay_seconds - elapsed
            logger.debug(f"Rate limiting: sleeping for {sleep_time:.2f}s")
            await asyncio.sleep(sleep_time)

        self._last_request_time = time.time()

    async def _get_sponsor_name(self, person_id: str) -> str | None:
        """
        Fetch the sponsor name format from OpenStates for bill filtering.

        The OpenStates bills API 'sponsor' parameter requires the family_name
        as used in sponsorship records, not the full name or person ID.

        Args:
            person_id: OpenStates person ID (e.g., "ocd-person/6a3fae94-...")

        Returns:
            Family name for sponsor filtering, or None if not found
        """
        await self._apply_rate_limit()

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                headers = {"x-api-key": self.api_key}
                response = await client.get(
                    f"{self.OPENSTATES_API_BASE}/people",
                    headers=headers,
                    params={"id": person_id},
                )

                if response.status_code == 200:
                    data = response.json()
                    results = data.get("results", [])
                    if results:
                        person = results[0]
                        # Use family_name which matches the sponsor format in bills
                        family_name = person.get("family_name")
                        if family_name:
                            return family_name
                        # Fallback to extracting from full name
                        name = person.get("name", "")
                        if name:
                            # Take the last word as family name
                            return name.split()[-1]

        except Exception as e:
            logger.warning(f"Failed to fetch sponsor name for {person_id}: {e}")

        return None

    async def fetch_sponsored_bills(
        self,
        person_id: str,
        jurisdiction: str,
        sponsor_name: str | None = None,
        per_page: int = 20,  # OpenStates limits to 20 when using sponsor filter
    ) -> list[dict[str, Any]]:
        """
        Fetch bills sponsored by a legislator from OpenStates.

        Args:
            person_id: OpenStates person ID (e.g., "ocd-person/6a3fae94-...")
            jurisdiction: Jurisdiction code (e.g., "fl", "us")
            sponsor_name: Family name for sponsor filtering (fetched if not provided)
            per_page: Number of results per page

        Returns:
            List of bill dicts from OpenStates
        """
        # Get sponsor name if not provided
        if not sponsor_name:
            sponsor_name = await self._get_sponsor_name(person_id)
            if not sponsor_name:
                logger.warning(f"Could not determine sponsor name for {person_id}")
                return []

        all_bills = []
        page = 1

        while True:
            await self._apply_rate_limit()

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    headers = {"x-api-key": self.api_key}
                    params = {
                        "sponsor": sponsor_name,
                        "jurisdiction": jurisdiction,
                        "per_page": per_page,
                        "page": page,
                        "include": "sponsorships",
                    }

                    response = await client.get(
                        f"{self.OPENSTATES_API_BASE}/bills",
                        headers=headers,
                        params=params,
                    )

                    if response.status_code == 429:
                        retry_after = int(response.headers.get("Retry-After", 5))
                        logger.warning(f"Rate limited, waiting {retry_after}s")
                        await asyncio.sleep(retry_after)
                        continue

                    response.raise_for_status()
                    data = response.json()

                    results = data.get("results", [])
                    if not results:
                        break

                    # Filter to only bills where this person is actually a sponsor
                    # (sponsor name search may match multiple people)
                    for bill in results:
                        sponsorships = bill.get("sponsorships", [])
                        for s in sponsorships:
                            person = s.get("person")
                            if person and person.get("id") == person_id:
                                all_bills.append(bill)
                                break

                    # Check pagination
                    pagination = data.get("pagination", {})
                    total_pages = pagination.get("max_page", 1)
                    if page >= total_pages:
                        break

                    page += 1

            except httpx.TimeoutException:
                logger.warning(f"Timeout fetching bills for {person_id}, page {page}")
                break
            except Exception as e:
                logger.error(f"Error fetching bills for {person_id}: {e}")
                break

        return all_bills

    def _find_bill_ddp_link(self, bill: dict) -> tuple[str | None, str | None]:
        """
        Find DDP link for a bill by matching identifier.

        Args:
            bill: OpenStates bill dict

        Returns:
            Tuple of (ddp_url, bill_name) or (None, None) if not found
        """
        identifier = bill.get("identifier", "")
        if not identifier:
            return None, None

        # Try to find in cache
        bill_info = self._bill_cache.get(identifier)
        if bill_info and bill_info.get("slug"):
            slug = bill_info["slug"]
            ddp_url = f"https://digitaldemocracyproject.org/bills/{slug}"
            return ddp_url, bill_info.get("name", "")

        return None, None

    def format_legislator_bills_chunk(
        self,
        legislator: dict,
        bills: list[dict[str, Any]],
    ) -> str:
        """
        Format a legislator's sponsored bills for RAG.

        Args:
            legislator: Legislator dict with name, party, chamber, etc.
            bills: List of bills from OpenStates

        Returns:
            Formatted markdown text for embedding
        """
        parts = []

        name = legislator.get("name", "Unknown")
        slug = legislator.get("slug", "")
        party = legislator.get("party", "")
        chamber = legislator.get("chamber", "")
        jurisdiction = legislator.get("jurisdiction", "")

        # Header with DDP link
        if slug:
            ddp_url = f"https://digitaldemocracyproject.org/legislators/{slug}"
            parts.append(f"## Bills Sponsored by [{name}]({ddp_url})")
        else:
            parts.append(f"## Bills Sponsored by {name}")

        # Basic info line
        info_parts = []
        if party:
            info_parts.append(f"**Party:** {party}")
        if chamber:
            info_parts.append(f"**Chamber:** {chamber}")
        if jurisdiction:
            info_parts.append(f"**State:** {jurisdiction.upper()}")
        if info_parts:
            parts.append(" | ".join(info_parts))

        if not bills:
            parts.append("\nNo sponsored bills found in the current legislative session.")
            return "\n".join(parts)

        # Separate primary sponsor and co-sponsor bills
        primary_bills = []
        cosponsor_bills = []

        for bill in bills:
            sponsorships = bill.get("sponsorships", [])
            person_id = legislator.get("openstates_id", "")

            is_primary = False
            for s in sponsorships:
                person = s.get("person")
                if person and isinstance(person, dict):
                    if person.get("id") == person_id and s.get("primary"):
                        is_primary = True
                        break

            if is_primary:
                primary_bills.append(bill)
            else:
                cosponsor_bills.append(bill)

        # Format primary sponsor bills
        if primary_bills:
            parts.append("\n### Bills as Primary Sponsor")
            for bill in primary_bills[:30]:  # Limit to 30
                formatted = self._format_bill_line(bill)
                parts.append(f"- {formatted}")
            if len(primary_bills) > 30:
                parts.append(f"  ...and {len(primary_bills) - 30} more bills")

        # Format co-sponsor bills
        if cosponsor_bills:
            parts.append("\n### Bills as Co-Sponsor")
            for bill in cosponsor_bills[:30]:  # Limit to 30
                formatted = self._format_bill_line(bill)
                parts.append(f"- {formatted}")
            if len(cosponsor_bills) > 30:
                parts.append(f"  ...and {len(cosponsor_bills) - 30} more bills")

        return "\n".join(parts)

    def _format_bill_line(self, bill: dict) -> str:
        """
        Format a single bill as a line item.

        Args:
            bill: OpenStates bill dict

        Returns:
            Formatted bill line (e.g., "[Bill Title](url) (HB 123) - Status")
        """
        title = bill.get("title", "Unknown Bill")
        identifier = bill.get("identifier", "")
        latest_action = bill.get("latest_action", {})
        status = latest_action.get("description", "") if latest_action else ""

        # Truncate long titles
        if len(title) > 80:
            title = title[:77] + "..."

        # Truncate long status descriptions
        if len(status) > 50:
            status = status[:47] + "..."

        # Try to find DDP link
        ddp_url, _ = self._find_bill_ddp_link(bill)

        if ddp_url:
            line = f"[{title}]({ddp_url})"
        else:
            line = title

        if identifier:
            line += f" ({identifier})"
        if status:
            line += f" - {status}"

        return line

    async def sync_legislator(
        self,
        legislator: dict,
    ) -> LegislatorSyncResult:
        """
        Sync a single legislator's sponsored bills.

        Args:
            legislator: Legislator dict with openstates_id, name, slug, etc.

        Returns:
            LegislatorSyncResult with sync status
        """
        openstates_id = legislator.get("openstates_id", "")
        name = legislator.get("name", "Unknown")
        jurisdiction = legislator.get("jurisdiction", "us").lower()

        if not openstates_id:
            return LegislatorSyncResult(
                legislator_id="",
                legislator_name=name,
                success=False,
                error="No OpenStates ID",
            )

        # Fetch sponsored bills
        bills = await self.fetch_sponsored_bills(
            person_id=openstates_id,
            jurisdiction=jurisdiction,
        )

        if not bills:
            logger.debug(f"No sponsored bills found for {name}")
            # Still create a document indicating no bills found
            # This is useful for RAG to know we checked

        # Format the content chunk
        content = self.format_legislator_bills_chunk(legislator, bills)

        # Create document metadata
        metadata = DocumentMetadata(
            document_id=f"legislator-bills-{openstates_id}",
            document_type="legislator-bills",
            source="openstates",
            title=f"{name} - Sponsored Bills",
            jurisdiction=jurisdiction.upper(),
            legislator_id=openstates_id,
            extra={
                "slug": legislator.get("slug", ""),
                "party": legislator.get("party", ""),
                "chamber": legislator.get("chamber", ""),
                "bills_count": len(bills),
                "last_synced": date.today().isoformat(),
            },
        )

        # Ingest the document
        try:
            result = await self.pipeline.ingest_document(
                content=content,
                metadata=metadata,
                skip_duplicates=False,  # Always update
            )

            return LegislatorSyncResult(
                legislator_id=openstates_id,
                legislator_name=name,
                success=True,
                bills_found=len(bills),
                chunks_created=result.chunks_created,
            )

        except Exception as e:
            logger.error(
                "Failed to ingest legislator bills",
                legislator_id=openstates_id,
                error=str(e),
            )
            return LegislatorSyncResult(
                legislator_id=openstates_id,
                legislator_name=name,
                success=False,
                error=str(e),
            )

    async def sync_all_legislators(
        self,
        legislators: list[dict],
    ) -> SyncBatchResult:
        """
        Sync sponsored bills for all legislators.

        Args:
            legislators: List of legislator dicts from Webflow with:
                - openstates_id: OpenStates person ID
                - name: Legislator name
                - slug: Webflow slug for DDP URL
                - jurisdiction: State code
                - party: Political party
                - chamber: Legislative chamber

        Returns:
            SyncBatchResult with aggregated results
        """
        import time

        # Build bill mapping for DDP links
        await self._build_bill_mapping()

        total = len(legislators)
        successful = 0
        failed = 0
        total_bills = 0
        chunks_created = 0
        errors = []
        start_time = time.time()

        logger.info(
            "Starting legislator bills sync",
            total_legislators=total,
            rate_limit_rpm=self.rate_limit.requests_per_minute,
        )

        for i, legislator in enumerate(legislators):
            name = legislator.get("name", "Unknown")

            result = await self.sync_legislator(legislator)

            if result.success:
                successful += 1
                total_bills += result.bills_found
                chunks_created += result.chunks_created
            else:
                failed += 1
                if result.error:
                    errors.append(f"{name}: {result.error}")

            # Progress logging every 10 legislators
            processed = successful + failed
            if processed > 0 and processed % 10 == 0:
                elapsed = time.time() - start_time
                rate = processed / elapsed if elapsed > 0 else 0
                logger.info(
                    "Legislator bills sync progress",
                    processed=processed,
                    total=total,
                    successful=successful,
                    failed=failed,
                    total_bills=total_bills,
                    rate_per_second=round(rate, 2),
                )

        # Final stats
        elapsed = time.time() - start_time
        elapsed_minutes = int(elapsed / 60)
        elapsed_seconds = int(elapsed % 60)

        logger.info(
            "Legislator bills sync complete",
            total_time=f"{elapsed_minutes}m {elapsed_seconds}s",
            successful=successful,
            failed=failed,
            total_bills=total_bills,
            chunks_created=chunks_created,
        )

        return SyncBatchResult(
            total_legislators=total,
            successful=successful,
            failed=failed,
            total_bills_found=total_bills,
            chunks_created=chunks_created,
            errors=errors,
        )
