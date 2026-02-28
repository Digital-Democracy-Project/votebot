"""OpenStates bill sync service for fetching status, votes, and actions."""

import asyncio
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import structlog
import yaml

from votebot.config import Settings, get_settings
from votebot.ingestion.metadata import DocumentMetadata
from votebot.ingestion.pipeline import DocumentSource, IngestionPipeline
from votebot.ingestion.sources.openstates import JurisdictionInfo, OpenStatesSource
from votebot.utils.legislative_calendar import StateLegislativeCalendar

logger = structlog.get_logger()

# Default config path
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "config" / "sync_schedule.yaml"


@dataclass
class RateLimitConfig:
    """Rate limiting configuration for OpenStates API.

    Defaults aligned with upgraded API tier: 30,000 calls/day, 2 calls/second.
    """

    requests_per_minute: int = 120  # 2 calls/second
    delay_between_bills_ms: int = 500  # 500ms minimum between requests
    max_retry_attempts: int = 3
    retry_backoff_seconds: int = 5


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
        # Add more as needed — new states are auto-resolved via OpenStates URL fallback
    }

    # Mapping of jurisdiction codes to human-readable source names
    JURISDICTION_SOURCE_NAMES = {
        "us": "US Congress",
        "fl": "Florida Legislature",
        "wa": "Washington Legislature",
        "va": "Virginia Legislature",
        "mi": "Michigan Legislature",
        "ma": "Massachusetts Legislature",
        "ut": "Utah Legislature",
        "az": "Arizona Legislature",
        "al": "Alabama Legislature",
        "ca": "California Legislature",
        "ny": "New York Legislature",
        "tx": "Texas Legislature",
        # Add more as needed
    }

    def __init__(self, settings: Settings | None = None, config_path: Path | None = None):
        """
        Initialize the bill sync service.

        Args:
            settings: Application settings
            config_path: Path to sync_schedule.yaml config file
        """
        self.settings = settings or get_settings()
        self.api_key = self.settings.openstates_api_key.get_secret_value()
        self.calendar = StateLegislativeCalendar()
        self.pipeline = IngestionPipeline(self.settings)
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self.rate_limit = self._load_rate_limit_config()
        self._last_request_time: float = 0
        # Cache for legislator lookups (openstates_id -> {name, slug})
        self._legislator_cache: dict[str, dict] = {}
        # Cache for jurisdiction info (jurisdiction -> JurisdictionInfo)
        self._jurisdiction_cache: dict[str, JurisdictionInfo] = {}
        # OpenStates source for jurisdiction lookups
        self._openstates_source = OpenStatesSource(self.settings)
        # Federal legislator cache for looking up person IDs by name (lazy loaded)
        self.__federal_cache = None

    @property
    def _federal_cache(self):
        """Lazily load the federal legislator cache to avoid circular imports."""
        if self.__federal_cache is None:
            from votebot.sync.federal_legislator_cache import get_federal_cache
            self.__federal_cache = get_federal_cache()
        return self.__federal_cache

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
                delay_between_bills_ms=rate_limit.get("delay_between_bills_ms", 100),
                max_retry_attempts=retry.get("max_attempts", 3),
                retry_backoff_seconds=retry.get("backoff_seconds", 5),
            )
        except Exception as e:
            logger.error(f"Failed to load rate limit config: {e}")
            return RateLimitConfig()

    def _get_source_name(self, jurisdiction: str) -> str:
        """
        Get a human-readable source name for a jurisdiction.

        Args:
            jurisdiction: Jurisdiction code (e.g., 'fl', 'us', 'wa')

        Returns:
            Human-readable source name (e.g., 'Florida Legislature', 'US Congress')
        """
        jurisdiction_lower = jurisdiction.lower() if jurisdiction else ""
        return self.JURISDICTION_SOURCE_NAMES.get(
            jurisdiction_lower,
            f"{jurisdiction.upper()} Legislature" if jurisdiction else "State Legislature"
        )

    def resolve_jurisdiction_code(self, jurisdiction_id: str, openstates_url: str = "") -> str:
        """Resolve Webflow jurisdiction reference ID to state code.

        Checks JURISDICTION_MAP first, falls back to parsing the OpenStates URL.
        """
        code = self.JURISDICTION_MAP.get(jurisdiction_id, "")
        if code:
            return code.upper()

        # Fallback: derive from OpenStates URL
        if openstates_url:
            parsed = self.parse_openstates_url(openstates_url)
            if parsed:
                logger.info(
                    "Resolved jurisdiction from OpenStates URL (not in JURISDICTION_MAP)",
                    jurisdiction=parsed.jurisdiction.upper(),
                    webflow_ref_id=jurisdiction_id,
                )
                return parsed.jurisdiction.upper()

        return ""

    async def _build_legislator_mapping(self) -> None:
        """
        Build a mapping from OpenStates person IDs to legislator info.

        Fetches the legislators collection from Webflow and creates a lookup table
        for resolving sponsor references to DDP URLs.
        """
        if self._legislator_cache:
            return  # Already cached

        legislators_collection_id = self.settings.webflow_legislators_collection_id
        if not legislators_collection_id:
            logger.warning("No legislators collection ID configured, skipping legislator mapping")
            return

        webflow_api_key = self.settings.webflow_votebot_api_key.get_secret_value()
        if not webflow_api_key:
            logger.warning("No Webflow API key configured, skipping legislator mapping")
            return

        logger.info("Building legislator mapping for sponsor linking...")

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
                        f"https://api.webflow.com/v2/collections/{legislators_collection_id}/items",
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
                        openstates_id = fields.get("openstatesid", "")
                        name = fields.get("name", "")
                        slug = fields.get("slug", "")

                        if openstates_id and slug:
                            self._legislator_cache[openstates_id] = {
                                "name": name,
                                "slug": slug,
                            }

                    pagination = data.get("pagination", {})
                    total = pagination.get("total", 0)
                    if len(self._legislator_cache) >= total or len(items) < page_size:
                        break

                    offset += page_size

            logger.info(
                f"Built legislator mapping with {len(self._legislator_cache)} entries"
            )

        except Exception as e:
            logger.warning(
                "Failed to build legislator mapping",
                error=str(e),
            )

    def _format_sponsor_with_link(self, sponsorship: dict) -> str:
        """
        Format a sponsor with optional DDP link.

        Args:
            sponsorship: OpenStates sponsorship dict with name and optional person.id

        Returns:
            Formatted sponsor string, with DDP link if available
        """
        name = sponsorship.get("name", "Unknown")

        # Check if we have a person ID to look up
        person = sponsorship.get("person")
        if person and isinstance(person, dict):
            person_id = person.get("id", "")
            if person_id:
                legislator_info = self._legislator_cache.get(person_id)
                if legislator_info and legislator_info.get("slug"):
                    slug = legislator_info["slug"]
                    ddp_url = f"https://digitaldemocracyproject.org/legislators/{slug}"
                    return f"[{name}]({ddp_url})"

        return name

    def _format_voter_with_link(self, legislator_id: str, name: str) -> str:
        """
        Format a voter name with optional DDP link.

        Args:
            legislator_id: OpenStates person ID (e.g., "ocd-person/...")
            name: Voter's display name

        Returns:
            Formatted voter string, with DDP link if available
        """
        if legislator_id:
            legislator_info = self._legislator_cache.get(legislator_id)
            if legislator_info and legislator_info.get("slug"):
                slug = legislator_info["slug"]
                ddp_url = f"https://digitaldemocracyproject.org/legislators/{slug}"
                return f"[{name}]({ddp_url})"
        return name

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

    async def _apply_rate_limit(self) -> None:
        """Apply rate limiting by sleeping if needed."""
        import time

        # Calculate minimum delay between requests
        min_delay_seconds = self.rate_limit.delay_between_bills_ms / 1000.0

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

    async def fetch_bill_from_openstates(
        self,
        jurisdiction: str,
        session: str,
        bill_id: str,
    ) -> dict[str, Any] | None:
        """
        Fetch bill details from OpenStates API with retry logic.

        Args:
            jurisdiction: State code (e.g., 'fl', 'wa')
            session: Session identifier (e.g., '2025', '119')
            bill_id: Bill identifier (e.g., 'HB 123')

        Returns:
            Bill data dict or None if not found
        """
        # Remove spaces from bill_id (OpenStates expects "HB363" not "HB 363" or "HB%20363")
        clean_bill_id = bill_id.replace(" ", "")
        url = f"{self.OPENSTATES_API_BASE}/bills/{jurisdiction}/{session}/{clean_bill_id}"

        logger.info(
            "Fetching bill from OpenStates",
            url=url,
            jurisdiction=jurisdiction,
            session=session,
            original_bill_id=bill_id,
            clean_bill_id=clean_bill_id,
        )

        last_error: Exception | None = None

        for attempt in range(self.rate_limit.max_retry_attempts):
            logger.info(
                "OpenStates fetch attempt",
                attempt=attempt + 1,
                max_attempts=self.rate_limit.max_retry_attempts,
                url=url,
                api_key_prefix=self.api_key[:8] + "..." if self.api_key else "MISSING",
            )

            # Apply rate limiting before each request
            await self._apply_rate_limit()

            try:
                async with httpx.AsyncClient(timeout=30.0, http2=False) as client:
                    # OpenStates v3 API requires include params to get detailed data
                    # Without these, only basic bill info is returned (no sponsors, actions, votes)
                    include_params = [
                        "sponsorships",
                        "abstracts",
                        "other_titles",
                        "other_identifiers",
                        "actions",
                        "sources",
                        "documents",
                        "versions",
                        "votes",
                        "related_bills",
                    ]
                    # Use header-based auth (more secure than query param)
                    headers = {
                        "accept": "application/json",
                        "x-api-key": self.api_key,
                    }
                    params = [("include", p) for p in include_params]
                    response = await client.get(url, headers=headers, params=params)

                    # Log the response for debugging
                    logger.info(
                        "OpenStates API response",
                        status_code=response.status_code,
                        url=str(response.url),
                    )

                    # Handle 404 - bill not found (don't retry)
                    if response.status_code == 404:
                        logger.warning(
                            "Bill not found in OpenStates",
                            jurisdiction=jurisdiction,
                            session=session,
                            bill_id=bill_id,
                        )
                        return None

                    # Handle rate limiting (429) - retry with backoff
                    if response.status_code == 429:
                        retry_after = int(response.headers.get("Retry-After", self.rate_limit.retry_backoff_seconds))
                        backoff = retry_after * (2 ** attempt)  # Exponential backoff
                        logger.warning(
                            "Rate limited by OpenStates (429), backing off",
                            attempt=attempt + 1,
                            backoff_seconds=backoff,
                            jurisdiction=jurisdiction,
                            bill_id=bill_id,
                        )
                        last_error = Exception(f"Rate limited (429)")
                        await asyncio.sleep(backoff)
                        continue

                    # Handle server errors (5xx) - retry with backoff
                    if response.status_code >= 500:
                        backoff = self.rate_limit.retry_backoff_seconds * (2 ** attempt)
                        logger.warning(
                            "OpenStates server error (5xx), retrying",
                            status_code=response.status_code,
                            attempt=attempt + 1,
                            backoff_seconds=backoff,
                            jurisdiction=jurisdiction,
                            bill_id=bill_id,
                        )
                        last_error = Exception(f"Server error ({response.status_code})")
                        await asyncio.sleep(backoff)
                        continue

                    # Raise for other error status codes
                    response.raise_for_status()
                    return response.json()

            except httpx.TimeoutException as e:
                last_error = e
                backoff = self.rate_limit.retry_backoff_seconds * (2 ** attempt)
                logger.warning(
                    "OpenStates request timeout, retrying",
                    attempt=attempt + 1,
                    backoff_seconds=backoff,
                    jurisdiction=jurisdiction,
                    bill_id=bill_id,
                )
                await asyncio.sleep(backoff)
                continue

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
                last_error = e
                logger.error(
                    "Failed to fetch from OpenStates",
                    error=str(e),
                    jurisdiction=jurisdiction,
                    session=session,
                    bill_id=bill_id,
                )
                return None

        # All retries exhausted
        logger.error(
            "OpenStates fetch failed after all retries",
            attempts=self.rate_limit.max_retry_attempts,
            last_error=str(last_error) if last_error else "Unknown",
            jurisdiction=jurisdiction,
            session=session,
            bill_id=bill_id,
        )
        return None

    def format_bill_history_chunk(
        self,
        bill_data: dict[str, Any],
        ddp_url: str | None = None,
    ) -> str:
        """
        Format bill data into a text chunk for RAG.

        Args:
            bill_data: OpenStates bill response
            ddp_url: Optional DDP URL for the bill page

        Returns:
            Formatted text chunk
        """
        parts = []

        # Header
        title = bill_data.get("title", "Unknown Bill")
        identifier = bill_data.get("identifier", "Unknown")
        jurisdiction = bill_data.get("jurisdiction", {})
        jurisdiction_name = jurisdiction.get("name", "Unknown") if isinstance(jurisdiction, dict) else str(jurisdiction)

        # Include DDP link if available
        if ddp_url:
            parts.append(f"## Legislative History: [{identifier}]({ddp_url})")
            parts.append(f"**Title:** [{title}]({ddp_url})")
        else:
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

        # Sponsors (with DDP links when available)
        sponsorships = bill_data.get("sponsorships", [])
        if sponsorships:
            parts.append(f"\n### Sponsors")
            primary = [s for s in sponsorships if s.get("primary")]
            secondary = [s for s in sponsorships if not s.get("primary")]

            if primary:
                primary_formatted = [self._format_sponsor_with_link(s) for s in primary]
                parts.append(f"**Primary Sponsor(s):** {', '.join(primary_formatted)}")

            if secondary:
                secondary_formatted = [self._format_sponsor_with_link(s) for s in secondary[:30]]
                if len(secondary) > 30:
                    secondary_formatted.append(f"and {len(secondary) - 30} others")
                parts.append(f"**Co-Sponsors:** {', '.join(secondary_formatted)}")

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

                # Individual votes (with DDP links when available)
                individual_votes = vote.get("votes", [])
                if individual_votes:
                    # Group by vote option
                    yes_voters = []
                    no_voters = []
                    other_voters = []

                    for v in individual_votes:
                        option = v.get("option", "").lower()
                        voter_name = v.get("voter_name", "Unknown")

                        # Extract person ID from nested voter object (OpenStates API structure)
                        voter_obj = v.get("voter", {})
                        person_id = ""
                        if isinstance(voter_obj, dict):
                            person_id = voter_obj.get("id", "")
                            # Use full name from voter object if available
                            if voter_obj.get("name"):
                                voter_name = voter_obj.get("name")

                        # Format with DDP link if available
                        formatted = self._format_voter_with_link(person_id, voter_name)

                        if option == "yes":
                            yes_voters.append(formatted)
                        elif option == "no":
                            no_voters.append(formatted)
                        else:
                            other_voters.append(f"{formatted} ({option})")

                    if yes_voters:
                        parts.append(f"**Voted Yes:** {', '.join(yes_voters[:20])}")
                        if len(yes_voters) > 20:
                            parts.append(f"  ...and {len(yes_voters) - 20} others")
                    if no_voters:
                        parts.append(f"**Voted No:** {', '.join(no_voters[:20])}")
                        if len(no_voters) > 20:
                            parts.append(f"  ...and {len(no_voters) - 20} others")
                    if other_voters:
                        parts.append(f"**Other:** {', '.join(other_voters[:10])}")
                        if len(other_voters) > 10:
                            parts.append(f"  ...and {len(other_voters) - 10} others")

        return "\n".join(parts)

    def format_bill_votes_chunk(
        self,
        bill_data: dict[str, Any],
        ddp_url: str | None = None,
    ) -> tuple[str, dict[str, Any]] | None:
        """
        Format bill votes into a dedicated text chunk for RAG.

        This creates a separate votes document for better retrieval when
        users ask specifically about voting records.

        Args:
            bill_data: OpenStates bill response
            ddp_url: Optional DDP URL for the bill page

        Returns:
            Tuple of (formatted text chunk, voter_ids dict) or None if no votes
            The voter_ids dict maps person IDs to voter info for metadata storage
        """
        votes = bill_data.get("votes", [])
        if not votes:
            return None

        parts = []
        # Track all voter person IDs for metadata
        all_voter_ids: dict[str, dict[str, str]] = {}
        # Track organization IDs from vote events
        organization_ids: list[str] = []

        # Header
        identifier = bill_data.get("identifier", "Unknown")
        title = bill_data.get("title", "Unknown Bill")
        openstates_bill_id = bill_data.get("id", "")
        jurisdiction = bill_data.get("jurisdiction", {})
        jurisdiction_name = jurisdiction.get("name", "Unknown") if isinstance(jurisdiction, dict) else str(jurisdiction)
        jurisdiction_id = jurisdiction.get("id", "") if isinstance(jurisdiction, dict) else ""

        if ddp_url:
            parts.append(f"## Voting Record: [{identifier}]({ddp_url})")
            parts.append(f"**Bill Title:** [{title}]({ddp_url})")
        else:
            parts.append(f"## Voting Record: {identifier}")
            parts.append(f"**Bill Title:** {title}")
        parts.append(f"**Jurisdiction:** {jurisdiction_name}")

        # Each vote event with full details
        for vote in votes:
            motion = vote.get("motion_text", "Vote")
            result = vote.get("result", "Unknown")
            vote_date = vote.get("start_date", "Unknown date")
            vote_event_id = vote.get("id", "")
            org = vote.get("organization", {})
            org_name = org.get("name", "Unknown") if isinstance(org, dict) else "Unknown"
            org_id = org.get("id", "") if isinstance(org, dict) else ""
            chamber = org.get("classification", "") if isinstance(org, dict) else ""

            if org_id and org_id not in organization_ids:
                organization_ids.append(org_id)

            # Count votes
            counts = vote.get("counts", [])
            yes_count = next((c.get("value", 0) for c in counts if c.get("option") == "yes"), 0)
            no_count = next((c.get("value", 0) for c in counts if c.get("option") == "no"), 0)
            other_count = sum(
                c.get("value", 0) for c in counts
                if c.get("option") not in ("yes", "no")
            )

            parts.append(f"\n### {org_name} - {vote_date}")
            if chamber:
                parts.append(f"**Chamber:** {chamber.title()}")
            parts.append(f"**Motion:** {motion}")
            parts.append(f"**Result:** {result.upper()} (Yes: {yes_count}, No: {no_count}, Other: {other_count})")

            # Individual votes (with DDP links when available)
            individual_votes = vote.get("votes", [])
            if individual_votes:
                # Group by vote option
                yes_voters = []
                no_voters = []
                other_voters: dict[str, list[str]] = {}

                # Check if this is a federal bill (for cache lookup)
                is_federal = jurisdiction_name.lower() in ("us", "united states")

                for v in individual_votes:
                    option = v.get("option", "").lower()
                    voter_name = v.get("voter_name", "Unknown")

                    # Extract person ID from nested voter object (OpenStates API structure)
                    voter_obj = v.get("voter", {})
                    person_id = ""
                    party = ""
                    if isinstance(voter_obj, dict):
                        person_id = voter_obj.get("id", "")
                        party = voter_obj.get("party", "")
                        # Use full name from voter object if available
                        if voter_obj.get("name"):
                            voter_name = voter_obj.get("name")

                    # For federal bills without person_id, look up from cache
                    if not person_id and is_federal:
                        cached_info = self._federal_cache.lookup_with_info(voter_name)
                        if cached_info:
                            person_id = cached_info.get("person_id", "")
                            # Use cached party if not available from vote
                            if not party:
                                party = cached_info.get("party", "")

                    # Track voter for metadata (keyed by person_id)
                    if person_id:
                        all_voter_ids[person_id] = {
                            "name": voter_name,
                            "party": party,
                            "option": option,
                        }

                    # Format with DDP link if available
                    formatted = self._format_voter_with_link(person_id, voter_name)

                    # Include party abbreviation for better parsing by reverse index
                    party_abbrev = ""
                    if party:
                        party_abbrev = {"Democratic": "D", "Republican": "R", "Independent": "I"}.get(party, party[0] if party else "")

                    # Build formatted name with available info
                    # Include person ID inline for reverse index (both state and federal)
                    # Check if voter_name already has party-state suffix (federal votes come as "Name (R-FL)")
                    import re
                    has_party_suffix = bool(re.search(r'\([RDI]-[A-Z]{2}\)$', voter_name))

                    if person_id and person_id.startswith("ocd-person/"):
                        # Has person ID - include ID in format: [id]Name (Party-State)
                        if has_party_suffix:
                            # voter_name already has party-state, just add person ID prefix
                            formatted_with_party = f"[{person_id}]{voter_name}"
                        elif party_abbrev:
                            # Need to add party-state suffix
                            if is_federal:
                                cached_info = self._federal_cache.lookup_with_info(voter_name)
                                state_code = cached_info.get("state", "") if cached_info else ""
                            else:
                                state_code = jurisdiction_name.upper()[:2] if len(jurisdiction_name) >= 2 else ""
                            if state_code and state_code.isalpha():
                                formatted_with_party = f"[{person_id}]{voter_name} ({party_abbrev}-{state_code})"
                            else:
                                formatted_with_party = f"[{person_id}]{voter_name} ({party_abbrev})"
                        else:
                            formatted_with_party = f"[{person_id}]{voter_name}"
                    elif has_party_suffix:
                        # Already has party-state suffix, use as-is
                        formatted_with_party = voter_name
                    elif party_abbrev:
                        # No person_id - use Name (Party-State) format
                        state_code = jurisdiction_name.upper()[:2] if len(jurisdiction_name) >= 2 else ""
                        if state_code and state_code.isalpha():
                            formatted_with_party = f"{voter_name} ({party_abbrev}-{state_code})"
                        else:
                            formatted_with_party = f"{voter_name} ({party_abbrev})"
                    else:
                        formatted_with_party = formatted

                    if option == "yes":
                        yes_voters.append(formatted_with_party)
                    elif option == "no":
                        no_voters.append(formatted_with_party)
                    else:
                        if option not in other_voters:
                            other_voters[option] = []
                        other_voters[option].append(formatted_with_party)

                # List all voters (increased limits for vote-specific document)
                if yes_voters:
                    parts.append(f"**Voted Yes ({len(yes_voters)}):** {', '.join(yes_voters[:100])}")
                    if len(yes_voters) > 100:
                        parts.append(f"  ...and {len(yes_voters) - 100} others")

                if no_voters:
                    parts.append(f"**Voted No ({len(no_voters)}):** {', '.join(no_voters[:100])}")
                    if len(no_voters) > 100:
                        parts.append(f"  ...and {len(no_voters) - 100} others")

                for option, voters in other_voters.items():
                    display_option = option.replace("_", " ").title()
                    parts.append(f"**{display_option} ({len(voters)}):** {', '.join(voters[:50])}")
                    if len(voters) > 50:
                        parts.append(f"  ...and {len(voters) - 50} others")

        # Build metadata for OpenStates IDs
        # Count voters with and without person IDs
        voters_with_ids = [pid for pid in all_voter_ids.keys() if pid.startswith("ocd-person/")]
        voters_without_ids = [pid for pid in all_voter_ids.keys() if not pid.startswith("ocd-person/")]

        ids_metadata = {
            "openstates_bill_id": openstates_bill_id,
            "openstates_jurisdiction_id": jurisdiction_id,
            "openstates_organization_ids": organization_ids,
            "voter_count": len(all_voter_ids),
            "voters_with_person_ids": len(voters_with_ids),
            "voters_without_person_ids": len(voters_without_ids),
            # Store person IDs separately (limited to avoid metadata size issues)
            # These are only available for state bills, not federal
            "voter_person_ids": voters_with_ids[:200] if voters_with_ids else [],
        }

        return "\n".join(parts), ids_metadata

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
            "webflow_id": webflow_bill_id,  # For filtering in RAG retrieval
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
        bill_slug: str | None = None,
    ) -> BillSyncResult:
        """
        Sync a single bill from OpenStates.

        Args:
            openstates_url: The open-states-url-2 field from Webflow
            webflow_bill_id: The Webflow item ID
            bill_title: Bill title from Webflow
            jurisdiction_name: Human-readable jurisdiction name
            bill_slug: Webflow slug for DDP URL generation

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

        # Build DDP URL if slug is available
        ddp_url = f"https://digitaldemocracyproject.org/bills/{bill_slug}" if bill_slug else None

        # Format the history chunk
        history_chunk = self.format_bill_history_chunk(bill_data, ddp_url=ddp_url)

        # Extract metadata
        extra_metadata = self.extract_metadata_from_openstates(bill_data, webflow_bill_id)
        # Add slug for RAG retrieval filtering
        if bill_slug:
            extra_metadata["slug"] = bill_slug

        # Create document for ingestion
        source_name = self._get_source_name(jurisdiction_name)
        metadata = DocumentMetadata(
            document_id=f"bill-history-{webflow_bill_id}",
            document_type="bill-history",
            source=source_name,
            title=f"{bill_title} - Legislative History",
            jurisdiction=jurisdiction_name,
            bill_id=parsed.bill_id,
            url=openstates_url,
            extra=extra_metadata,
        )

        # Ingest the history document
        total_chunks = 0
        try:
            result = await self.pipeline.ingest_document(
                content=history_chunk,
                metadata=metadata,
                skip_duplicates=False,  # Always update
            )
            total_chunks += result.chunks_created

            # Also create a dedicated votes document if votes exist
            votes_result_data = self.format_bill_votes_chunk(bill_data, ddp_url=ddp_url)
            if votes_result_data:
                votes_chunk, votes_ids_metadata = votes_result_data
                votes_metadata = DocumentMetadata(
                    document_id=f"bill-votes-{webflow_bill_id}",
                    document_type="bill-votes",
                    source=source_name,
                    title=f"{bill_title} - Voting Record",
                    jurisdiction=jurisdiction_name,
                    bill_id=parsed.bill_id,
                    url=openstates_url,
                    extra={
                        **extra_metadata,
                        "vote_count": len(bill_data.get("votes", [])),
                        # Include OpenStates IDs for linking
                        "openstates_bill_id": votes_ids_metadata.get("openstates_bill_id", ""),
                        "openstates_jurisdiction_id": votes_ids_metadata.get("openstates_jurisdiction_id", ""),
                        "openstates_organization_ids": votes_ids_metadata.get("openstates_organization_ids", []),
                        "voter_count": votes_ids_metadata.get("voter_count", 0),
                        "voter_person_ids": votes_ids_metadata.get("voter_person_ids", []),
                    },
                )
                votes_result = await self.pipeline.ingest_document(
                    content=votes_chunk,
                    metadata=votes_metadata,
                    skip_duplicates=False,
                )
                total_chunks += votes_result.chunks_created
                logger.debug(
                    "Bill votes document created",
                    webflow_bill_id=webflow_bill_id,
                    vote_chunks=votes_result.chunks_created,
                    voter_count=votes_ids_metadata.get("voter_count", 0),
                )

            return BillSyncResult(
                bill_id=webflow_bill_id,
                jurisdiction=jurisdiction_name,
                success=True,
                chunks_created=total_chunks,
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

    async def get_jurisdiction_info(self, jurisdiction: str) -> JurisdictionInfo | None:
        """
        Get jurisdiction info from OpenStates API (cached).

        Args:
            jurisdiction: State code (e.g., "FL", "US")

        Returns:
            JurisdictionInfo or None if not available
        """
        jurisdiction_lower = jurisdiction.lower()

        # Check cache first
        if jurisdiction_lower in self._jurisdiction_cache:
            return self._jurisdiction_cache[jurisdiction_lower]

        # Fetch from API
        info = await self._openstates_source.fetch_jurisdiction(jurisdiction_lower)
        if info:
            self._jurisdiction_cache[jurisdiction_lower] = info
            logger.info(
                "Cached jurisdiction info",
                jurisdiction=jurisdiction,
                current_session=info.get_current_session().identifier if info.get_current_session() else None,
                latest_bill_update=str(info.latest_bill_update) if info.latest_bill_update else None,
            )

        return info

    async def is_current_session_async(
        self,
        session_year: str | None,
        session_code: str | None,
        jurisdiction: str,
    ) -> bool:
        """
        Determine if a bill is from the current legislative session (async version).

        Uses OpenStates API for accurate session detection when available.

        Args:
            session_year: Session year from Webflow (e.g., "2025", "2025-2026")
            session_code: Session code from Webflow (e.g., "2025", "119")
            jurisdiction: State code (e.g., "FL", "US")

        Returns:
            True if this is a current session bill
        """
        # Try to get accurate session info from OpenStates API
        info = await self.get_jurisdiction_info(jurisdiction)
        if info:
            current_session = info.get_current_session()
            if current_session:
                # Check if the bill's session matches the current session
                if session_code and session_code == current_session.identifier:
                    return True
                # Also check if session_year matches (some use year format)
                if session_year and session_year == current_session.identifier:
                    return True
                # Check if the session identifier is in the session_year string
                if session_year and current_session.identifier in session_year:
                    return True

        # Fall back to heuristic-based detection
        return self.is_current_session(session_year, session_code, jurisdiction)

    def is_current_session(
        self,
        session_year: str | None,
        session_code: str | None,
        jurisdiction: str,
    ) -> bool:
        """
        Determine if a bill is from the current legislative session.

        Uses year-based heuristics. For more accurate detection, use
        is_current_session_async() which queries the OpenStates API.

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
        heartbeat_callback: Any | None = None,
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
        # Build legislator mapping for sponsor DDP links
        await self._build_legislator_mapping()

        # Warm legislative calendar with live OpenStates session data
        jurisdiction_codes = set()
        for bill in bills:
            fields = bill.get("fieldData", {})
            jurisdiction_id = fields.get("jurisdiction", "")
            openstates_url = fields.get("open-states-url-2", "")
            code = self.resolve_jurisdiction_code(jurisdiction_id, openstates_url)
            if code:
                jurisdiction_codes.add(code)

        jurisdiction_data = {}
        for code in jurisdiction_codes:
            try:
                info = await self.get_jurisdiction_info(code)
                if info:
                    jurisdiction_data[code] = info
            except Exception as e:
                logger.warning("Failed to fetch jurisdiction for calendar warm", state=code, error=str(e))

        if jurisdiction_data:
            self.calendar.warm_cache(jurisdiction_data)

        # Track active jurisdictions in Redis for cross-worker visibility
        if jurisdiction_codes:
            from votebot.services.redis_store import get_redis_store

            redis_store = get_redis_store()
            for code in jurisdiction_codes:
                await redis_store.add_active_jurisdiction(code)

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
            slug = fields.get("slug", "")

            # Get jurisdiction code
            jurisdiction_code = self.resolve_jurisdiction_code(jurisdiction_id, openstates_url)

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
                bill_slug=slug,
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

            # Keep heartbeat alive during long-running OpenStates phase
            if heartbeat_callback and (successful + failed) % 10 == 0:
                await heartbeat_callback()

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
        import time

        # Build legislator mapping for sponsor DDP links
        await self._build_legislator_mapping()

        total = len(bills)
        successful = 0
        failed = 0
        skipped = 0
        chunks_created = 0
        errors = []
        start_time = time.time()

        # Count bills with OpenStates URLs for better progress tracking
        bills_with_urls = [b for b in bills if b.get("fieldData", {}).get("open-states-url-2")]
        processable_count = len(bills_with_urls)

        logger.info(
            "Starting backload",
            total_bills=total,
            bills_with_openstates_urls=processable_count,
            rate_limit_rpm=self.rate_limit.requests_per_minute,
        )

        for i, bill in enumerate(bills):
            fields = bill.get("fieldData", {})
            webflow_id = bill.get("id", "")
            title = fields.get("name", "Unknown")
            openstates_url = fields.get("open-states-url-2", "")
            jurisdiction_id = fields.get("jurisdiction", "")
            slug = fields.get("slug", "")

            # Get jurisdiction code
            jurisdiction_code = self.resolve_jurisdiction_code(jurisdiction_id, openstates_url)
            if not jurisdiction_code:
                jurisdiction_code = jurisdiction_id[:8]  # Preserve existing truncation fallback

            # Skip bills without OpenStates URL
            if not openstates_url:
                skipped += 1
                logger.debug(f"Skipping bill without OpenStates URL: {title}")
                continue

            # Sync the bill
            result = await self.sync_bill(
                openstates_url=openstates_url,
                webflow_bill_id=webflow_id,
                bill_title=title,
                jurisdiction_name=jurisdiction_code,
                bill_slug=slug,
            )

            if result.success:
                successful += 1
                chunks_created += result.chunks_created
            else:
                failed += 1
                if result.error:
                    errors.append(f"{title}: {result.error}")

            # Progress logging every 25 bills
            processed = successful + failed
            if processed > 0 and processed % 25 == 0:
                elapsed = time.time() - start_time
                rate = processed / elapsed if elapsed > 0 else 0
                remaining = processable_count - processed
                eta_seconds = remaining / rate if rate > 0 else 0
                eta_minutes = int(eta_seconds / 60)
                eta_seconds_rem = int(eta_seconds % 60)

                logger.info(
                    "Backload progress",
                    processed=processed,
                    total_processable=processable_count,
                    successful=successful,
                    failed=failed,
                    skipped=skipped,
                    rate_per_second=round(rate, 2),
                    eta=f"{eta_minutes}m {eta_seconds_rem}s",
                )

        # Final stats
        elapsed = time.time() - start_time
        elapsed_minutes = int(elapsed / 60)
        elapsed_seconds = int(elapsed % 60)

        logger.info(
            "Backload complete",
            total_time=f"{elapsed_minutes}m {elapsed_seconds}s",
            successful=successful,
            failed=failed,
            skipped=skipped,
            chunks_created=chunks_created,
        )

        return SyncBatchResult(
            total_bills=total,
            successful=successful,
            failed=failed,
            chunks_created=chunks_created,
            errors=errors,
        )
