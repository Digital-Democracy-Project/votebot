"""OpenStates bill sync service for fetching status, votes, and actions."""

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, AsyncIterator

import httpx
import structlog

from votebot.config import Settings, get_settings
from votebot.ingestion.metadata import DocumentMetadata
from votebot.ingestion.pipeline import DocumentSource, IngestionPipeline
from votebot.utils.legislative_calendar import StateLegislativeCalendar

logger = structlog.get_logger()


@dataclass
class OpenStatesUrl:
    """Parsed OpenStates URL components."""

    jurisdiction: str
    session: str
    bill_id: str
    original_url: str


@dataclass
class BillSyncResult:
    """Result of syncing a single bill."""

    bill_id: str
    jurisdiction: str
    success: bool
    chunks_created: int = 0
    error: str | None = None


@dataclass
class SyncBatchResult:
    """Result of syncing a batch of bills."""

    total_bills: int
    successful: int
    failed: int
    chunks_created: int
    errors: list[str]


class BillSyncService:
    """
    Service for syncing bill data from OpenStates API v3.

    Fetches:
    - Current status and latest action
    - Full action history
    - Vote results
    - Sponsorship information
    """

    OPENSTATES_API_BASE = "https://v3.openstates.org"

    # Mapping of Webflow jurisdiction IDs to OpenStates jurisdiction codes
    JURISDICTION_MAP = {
        "655288ef928edb128306745f": "fl",
        "65810f6b889af86635a71b49": "us",
        "691294466973f77ba7924c9b": "wa",
        "6912910d68fa6adb1b2b630f": "va",
        "6912929f5ec63fd925b99c10": "mi",
        "6912928fd6eec8ac6bccb2c8": "ma",
        "69129425a577496525c8e52a": "ut",
        "6912916752bfa901425f1e76": "az",
        "69129146d6eec8ac6bcc8280": "al",
        # Add more as needed
    }

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the bill sync service.

        Args:
            settings: Application settings
        """
        self.settings = settings or get_settings()
        self.api_key = self.settings.openstates_api_key.get_secret_value()
        self.calendar = StateLegislativeCalendar()
        self.pipeline = IngestionPipeline(self.settings)

    def parse_openstates_url(self, url: str) -> OpenStatesUrl | None:
        """
        Parse an OpenStates URL to extract components.

        Args:
            url: OpenStates URL like https://openstates.org/fl/bills/2025/HB123/

        Returns:
            OpenStatesUrl with parsed components, or None if invalid
        """
        if not url:
            return None

        # Pattern: https://openstates.org/{jurisdiction}/bills/{session}/{bill_id}/
        pattern = r"https?://openstates\.org/([a-z]{2})/bills/([^/]+)/([^/]+)/?"
        match = re.match(pattern, url, re.IGNORECASE)

        if not match:
            return None

        return OpenStatesUrl(
            jurisdiction=match.group(1).lower(),
            session=match.group(2),
            bill_id=match.group(3),
            original_url=url,
        )

    async def fetch_bill_from_openstates(
        self,
        jurisdiction: str,
        session: str,
        bill_id: str,
    ) -> dict[str, Any] | None:
        """
        Fetch bill details from OpenStates API.

        Args:
            jurisdiction: State code (e.g., 'fl', 'wa')
            session: Session identifier (e.g., '2025', '119')
            bill_id: Bill identifier (e.g., 'HB 123')

        Returns:
            Bill data dict or None if not found
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = {"x-api-key": self.api_key}

            # URL encode the bill_id (spaces become %20)
            encoded_bill_id = bill_id.replace(" ", "%20")

            url = f"{self.OPENSTATES_API_BASE}/bills/{jurisdiction}/{session}/{encoded_bill_id}"

            try:
                response = await client.get(
                    url,
                    headers=headers,
                    params={"include": "votes,sponsorships,actions"},
                )

                if response.status_code == 404:
                    logger.warning(
                        "Bill not found in OpenStates",
                        jurisdiction=jurisdiction,
                        session=session,
                        bill_id=bill_id,
                    )
                    return None

                response.raise_for_status()
                return response.json()

            except httpx.HTTPStatusError as e:
                logger.error(
                    "OpenStates API error",
                    status_code=e.response.status_code,
                    jurisdiction=jurisdiction,
                    session=session,
                    bill_id=bill_id,
                )
                return None
            except Exception as e:
                logger.error(
                    "Failed to fetch from OpenStates",
                    error=str(e),
                    jurisdiction=jurisdiction,
                    session=session,
                    bill_id=bill_id,
                )
                return None

    def format_bill_history_chunk(self, bill_data: dict[str, Any]) -> str:
        """
        Format bill data into a text chunk for RAG.

        Args:
            bill_data: OpenStates bill response

        Returns:
            Formatted text chunk
        """
        parts = []

        # Header
        title = bill_data.get("title", "Unknown Bill")
        identifier = bill_data.get("identifier", "Unknown")
        jurisdiction = bill_data.get("jurisdiction", {})
        jurisdiction_name = jurisdiction.get("name", "Unknown") if isinstance(jurisdiction, dict) else str(jurisdiction)

        parts.append(f"## Legislative History: {identifier}")
        parts.append(f"**Title:** {title}")
        parts.append(f"**Jurisdiction:** {jurisdiction_name}")

        # Current Status
        latest_action = bill_data.get("latest_action")
        if latest_action:
            action_date = latest_action.get("date", "Unknown date")
            action_desc = latest_action.get("description", "Unknown action")
            parts.append(f"\n### Current Status")
            parts.append(f"**Latest Action ({action_date}):** {action_desc}")

        # Sponsors
        sponsorships = bill_data.get("sponsorships", [])
        if sponsorships:
            parts.append(f"\n### Sponsors")
            primary = [s for s in sponsorships if s.get("primary")]
            secondary = [s for s in sponsorships if not s.get("primary")]

            if primary:
                primary_names = [s.get("name", "Unknown") for s in primary]
                parts.append(f"**Primary Sponsor(s):** {', '.join(primary_names)}")

            if secondary:
                secondary_names = [s.get("name", "Unknown") for s in secondary[:10]]  # Limit to 10
                if len(secondary) > 10:
                    secondary_names.append(f"and {len(secondary) - 10} others")
                parts.append(f"**Co-Sponsors:** {', '.join(secondary_names)}")

        # Action History
        actions = bill_data.get("actions", [])
        if actions:
            parts.append(f"\n### Action History")
            # Sort by date descending (most recent first)
            sorted_actions = sorted(
                actions,
                key=lambda x: x.get("date", ""),
                reverse=True,
            )
            for action in sorted_actions[:20]:  # Limit to 20 actions
                action_date = action.get("date", "Unknown")
                action_desc = action.get("description", "Unknown")
                org = action.get("organization", {})
                org_name = org.get("name", "") if isinstance(org, dict) else ""

                if org_name:
                    parts.append(f"- **{action_date}** ({org_name}): {action_desc}")
                else:
                    parts.append(f"- **{action_date}**: {action_desc}")

        # Votes
        votes = bill_data.get("votes", [])
        if votes:
            parts.append(f"\n### Vote Results")
            for vote in votes:
                motion = vote.get("motion_text", "Vote")
                result = vote.get("result", "Unknown")
                vote_date = vote.get("start_date", "Unknown date")
                org = vote.get("organization", {})
                org_name = org.get("name", "Unknown") if isinstance(org, dict) else "Unknown"

                # Count votes
                counts = vote.get("counts", [])
                yes_count = next((c.get("value", 0) for c in counts if c.get("option") == "yes"), 0)
                no_count = next((c.get("value", 0) for c in counts if c.get("option") == "no"), 0)

                parts.append(f"\n**{org_name} - {vote_date}**")
                parts.append(f"Motion: {motion}")
                parts.append(f"Result: **{result.upper()}** (Yes: {yes_count}, No: {no_count})")

        return "\n".join(parts)

    def extract_metadata_from_openstates(
        self,
        bill_data: dict[str, Any],
        webflow_bill_id: str,
    ) -> dict[str, Any]:
        """
        Extract metadata fields from OpenStates bill data.

        Args:
            bill_data: OpenStates bill response
            webflow_bill_id: Original Webflow bill ID

        Returns:
            Metadata dict for Pinecone
        """
        latest_action = bill_data.get("latest_action", {})

        # Determine status from latest action classification
        classifications = latest_action.get("classification", []) if latest_action else []
        status = "unknown"
        if "became-law" in classifications or "governor-signed" in classifications:
            status = "signed"
        elif "passed" in classifications:
            status = "passed"
        elif "failed" in classifications or "vetoed" in classifications:
            status = "failed"
        elif "introduced" in classifications or "filed" in classifications:
            status = "introduced"
        elif "referred-to-committee" in classifications:
            status = "in_committee"

        return {
            "bill_status": status,
            "latest_action_date": latest_action.get("date") if latest_action else None,
            "latest_action_description": latest_action.get("description", "")[:200] if latest_action else None,
            "openstates_id": bill_data.get("id"),
            "last_synced": date.today().isoformat(),
        }

    async def sync_bill(
        self,
        openstates_url: str,
        webflow_bill_id: str,
        bill_title: str,
        jurisdiction_name: str,
    ) -> BillSyncResult:
        """
        Sync a single bill from OpenStates.

        Args:
            openstates_url: The open-states-url-2 field from Webflow
            webflow_bill_id: The Webflow item ID
            bill_title: Bill title from Webflow
            jurisdiction_name: Human-readable jurisdiction name

        Returns:
            BillSyncResult with sync status
        """
        # Parse the OpenStates URL
        parsed = self.parse_openstates_url(openstates_url)
        if not parsed:
            return BillSyncResult(
                bill_id=webflow_bill_id,
                jurisdiction=jurisdiction_name,
                success=False,
                error=f"Invalid OpenStates URL: {openstates_url}",
            )

        # Fetch from OpenStates API
        bill_data = await self.fetch_bill_from_openstates(
            parsed.jurisdiction,
            parsed.session,
            parsed.bill_id,
        )

        if not bill_data:
            return BillSyncResult(
                bill_id=webflow_bill_id,
                jurisdiction=jurisdiction_name,
                success=False,
                error="Bill not found in OpenStates",
            )

        # Format the history chunk
        history_chunk = self.format_bill_history_chunk(bill_data)

        # Extract metadata
        extra_metadata = self.extract_metadata_from_openstates(bill_data, webflow_bill_id)

        # Create document for ingestion
        metadata = DocumentMetadata(
            document_id=f"bill-history-{webflow_bill_id}",
            document_type="bill-history",
            source="openstates",
            title=f"{bill_title} - Legislative History",
            jurisdiction=jurisdiction_name,
            bill_id=parsed.bill_id,
            url=openstates_url,
            extra=extra_metadata,
        )

        # Ingest the document
        try:
            result = await self.pipeline.ingest_document(
                content=history_chunk,
                metadata=metadata,
                skip_duplicates=False,  # Always update
            )

            return BillSyncResult(
                bill_id=webflow_bill_id,
                jurisdiction=jurisdiction_name,
                success=True,
                chunks_created=result.chunks_created,
            )

        except Exception as e:
            logger.error(
                "Failed to ingest bill history",
                webflow_bill_id=webflow_bill_id,
                error=str(e),
            )
            return BillSyncResult(
                bill_id=webflow_bill_id,
                jurisdiction=jurisdiction_name,
                success=False,
                error=str(e),
            )

    def is_current_session(
        self,
        session_year: str | None,
        session_code: str | None,
        jurisdiction: str,
    ) -> bool:
        """
        Determine if a bill is from the current legislative session.

        Args:
            session_year: Session year from Webflow (e.g., "2025", "2025-2026")
            session_code: Session code from Webflow (e.g., "2025", "119")
            jurisdiction: State code (e.g., "FL", "US")

        Returns:
            True if this is a current session bill
        """
        current_year = date.today().year

        # Handle federal congress (e.g., session_code="119")
        if jurisdiction.upper() == "US":
            # 119th Congress: 2025-2027
            # 118th Congress: 2023-2025
            # Congress number = (year - 1789) / 2 + 1
            if session_code and session_code.isdigit():
                congress_num = int(session_code)
                congress_start_year = 1789 + (congress_num - 1) * 2
                congress_end_year = congress_start_year + 2
                return congress_start_year <= current_year <= congress_end_year

        # Handle state sessions by year
        if session_year:
            # Parse years from formats like "2025", "2025-2026", "2025-2027"
            years = re.findall(r"\d{4}", session_year)
            if years:
                years = [int(y) for y in years]
                # Current if we're within the session years
                if current_year in years or (len(years) > 1 and years[0] <= current_year <= years[-1]):
                    return True

        # Fall back to checking session_code as year
        if session_code:
            years = re.findall(r"\d{4}", session_code)
            if years:
                years = [int(y) for y in years]
                if current_year in years or (len(years) > 1 and years[0] <= current_year <= years[-1]):
                    return True

        return False

    def should_sync_jurisdiction(self, jurisdiction_code: str) -> bool:
        """
        Determine if we should sync bills for this jurisdiction today.

        Args:
            jurisdiction_code: Two-letter state code (e.g., "FL", "WA")

        Returns:
            True if we should sync today
        """
        # Always sync federal
        if jurisdiction_code.upper() == "US":
            return True

        # Check if state is currently in session
        try:
            if self.calendar.is_in_session(jurisdiction_code):
                return True
        except ValueError:
            # Unknown state, default to syncing
            return True

        # Even if not in session, sync on Mondays to catch pre-filed bills
        return date.today().weekday() == 0

    async def sync_current_session_bills(
        self,
        bills: list[dict[str, Any]],
    ) -> SyncBatchResult:
        """
        Sync bills from the current session only.

        Args:
            bills: List of bill dicts from Webflow with fields:
                   - id: Webflow item ID
                   - name: Bill title
                   - open-states-url-2: OpenStates URL
                   - session-year: Session year
                   - session-code: Session code
                   - jurisdiction: Jurisdiction reference

        Returns:
            SyncBatchResult with aggregated results
        """
        total = len(bills)
        successful = 0
        failed = 0
        chunks_created = 0
        errors = []

        for bill in bills:
            fields = bill.get("fieldData", {})
            webflow_id = bill.get("id", "")
            title = fields.get("name", "Unknown")
            openstates_url = fields.get("open-states-url-2", "")
            session_year = fields.get("session-year", "")
            session_code = fields.get("session-code", "")
            jurisdiction_id = fields.get("jurisdiction", "")

            # Get jurisdiction code
            jurisdiction_code = self.JURISDICTION_MAP.get(jurisdiction_id, "")

            # Skip bills without OpenStates URL
            if not openstates_url:
                logger.debug(f"Skipping bill without OpenStates URL: {title}")
                continue

            # Check if current session (for daily sync)
            if not self.is_current_session(session_year, session_code, jurisdiction_code):
                logger.debug(f"Skipping non-current session bill: {title}")
                continue

            # Check if we should sync this jurisdiction today
            if not self.should_sync_jurisdiction(jurisdiction_code):
                logger.debug(f"Skipping jurisdiction not scheduled for today: {jurisdiction_code}")
                continue

            # Sync the bill
            result = await self.sync_bill(
                openstates_url=openstates_url,
                webflow_bill_id=webflow_id,
                bill_title=title,
                jurisdiction_name=jurisdiction_code,
            )

            if result.success:
                successful += 1
                chunks_created += result.chunks_created
            else:
                failed += 1
                if result.error:
                    errors.append(f"{title}: {result.error}")

            logger.info(
                "Bill sync result",
                bill=title[:50],
                success=result.success,
                error=result.error,
            )

        return SyncBatchResult(
            total_bills=total,
            successful=successful,
            failed=failed,
            chunks_created=chunks_created,
            errors=errors,
        )

    async def backload_all_bills(
        self,
        bills: list[dict[str, Any]],
    ) -> SyncBatchResult:
        """
        Backload all bills regardless of session (one-time operation).

        Args:
            bills: List of all bill dicts from Webflow

        Returns:
            SyncBatchResult with aggregated results
        """
        total = len(bills)
        successful = 0
        failed = 0
        chunks_created = 0
        errors = []

        for i, bill in enumerate(bills):
            fields = bill.get("fieldData", {})
            webflow_id = bill.get("id", "")
            title = fields.get("name", "Unknown")
            openstates_url = fields.get("open-states-url-2", "")
            jurisdiction_id = fields.get("jurisdiction", "")

            # Get jurisdiction code
            jurisdiction_code = self.JURISDICTION_MAP.get(jurisdiction_id, jurisdiction_id[:8])

            # Skip bills without OpenStates URL
            if not openstates_url:
                logger.debug(f"Skipping bill without OpenStates URL: {title}")
                continue

            # Sync the bill
            result = await self.sync_bill(
                openstates_url=openstates_url,
                webflow_bill_id=webflow_id,
                bill_title=title,
                jurisdiction_name=jurisdiction_code,
            )

            if result.success:
                successful += 1
                chunks_created += result.chunks_created
            else:
                failed += 1
                if result.error:
                    errors.append(f"{title}: {result.error}")

            # Progress logging
            if (i + 1) % 50 == 0:
                logger.info(
                    "Backload progress",
                    processed=i + 1,
                    total=total,
                    successful=successful,
                    failed=failed,
                )

        return SyncBatchResult(
            total_bills=total,
            successful=successful,
            failed=failed,
            chunks_created=chunks_created,
            errors=errors,
        )
