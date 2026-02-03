"""Legislator sync service for fetching sponsored bills and voting records from OpenStates."""

import asyncio
from dataclasses import dataclass, field
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
    """Rate limiting configuration for OpenStates API.

    Defaults aligned with upgraded API tier: 30,000 calls/day, 2 calls/second.
    """

    requests_per_minute: int = 120  # 2 calls/second
    delay_between_requests_ms: int = 500  # 500ms minimum between requests
    max_retry_attempts: int = 3
    retry_backoff_seconds: int = 5


@dataclass
class LegislatorSyncResult:
    """Result of syncing a single legislator's sponsored bills and votes."""

    legislator_id: str
    legislator_name: str
    success: bool
    bills_found: int = 0
    votes_found: int = 0
    chunks_created: int = 0
    error: str | None = None


@dataclass
class LegislatorVote:
    """A single vote cast by a legislator."""

    bill_identifier: str
    bill_title: str
    vote_option: str  # "yes", "no", "not voting", "excused", etc.
    vote_date: str
    motion_text: str
    vote_result: str  # "pass", "fail"
    chamber: str  # "upper", "lower"
    bill_ddp_url: str | None = None


@dataclass
class SyncBatchResult:
    """Result of syncing a batch of legislators."""

    total_legislators: int
    successful: int
    failed: int
    total_bills_found: int
    total_votes_found: int
    chunks_created: int
    errors: list[str] = field(default_factory=list)


class LegislatorSyncService:
    """
    Service for syncing legislator data from OpenStates API v3.

    Creates separate documents for:
    - `legislator-bills`: Bills sponsored by the legislator
    - `legislator-votes`: Voting record (how they voted on bills)

    This enables queries like:
    - "What bills has Rick Scott sponsored?"
    - "How did Senator Smith vote on healthcare bills?"
    - "What is Representative Jones's voting record?"
    """

    OPENSTATES_API_BASE = "https://v3.openstates.org"

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
    }

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
                headers = {
                    "accept": "application/json",
                    "x-api-key": self.api_key,
                }
                # Include all available data for legislators
                include_params = [
                    "other_names",
                    "other_identifiers",
                    "links",
                    "sources",
                    "offices",
                ]
                params = [("id", person_id)] + [("include", p) for p in include_params]
                response = await client.get(
                    f"{self.OPENSTATES_API_BASE}/people",
                    headers=headers,
                    params=params,
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

    async def fetch_legislator_votes(
        self,
        person_id: str,
        jurisdiction: str,
        session: str | None = None,
        max_bills: int = 200,
    ) -> list[LegislatorVote]:
        """
        Fetch voting record for a legislator from OpenStates.

        Since OpenStates doesn't have a direct votes-by-person endpoint,
        this fetches bills with votes and extracts the legislator's votes.

        Args:
            person_id: OpenStates person ID (e.g., "ocd-person/6a3fae94-...")
            jurisdiction: Jurisdiction code (e.g., "fl", "us")
            session: Optional session filter (e.g., "2026", "119")
            max_bills: Maximum bills to fetch (to limit API calls)

        Returns:
            List of LegislatorVote objects
        """
        all_votes: list[LegislatorVote] = []
        page = 1
        bills_fetched = 0

        logger.info(
            "Fetching voting record",
            person_id=person_id,
            jurisdiction=jurisdiction,
            session=session,
            max_bills=max_bills,
        )

        while bills_fetched < max_bills:
            await self._apply_rate_limit()

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    headers = {"x-api-key": self.api_key}
                    params: dict[str, Any] = {
                        "jurisdiction": jurisdiction,
                        "per_page": 50,
                        "page": page,
                        "include": "votes",
                    }
                    if session:
                        params["session"] = session

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

                    # Extract votes for this legislator from each bill
                    for bill in results:
                        bills_fetched += 1
                        bill_votes = self._extract_legislator_votes_from_bill(
                            bill, person_id
                        )
                        all_votes.extend(bill_votes)

                        if bills_fetched >= max_bills:
                            break

                    # Check pagination
                    pagination = data.get("pagination", {})
                    total_pages = pagination.get("max_page", 1)
                    if page >= total_pages:
                        break

                    page += 1

            except httpx.TimeoutException:
                logger.warning(f"Timeout fetching votes for {person_id}, page {page}")
                break
            except Exception as e:
                logger.error(f"Error fetching votes for {person_id}: {e}")
                break

        logger.info(
            "Fetched voting record",
            person_id=person_id,
            bills_checked=bills_fetched,
            votes_found=len(all_votes),
        )

        return all_votes

    def _extract_legislator_votes_from_bill(
        self,
        bill: dict,
        person_id: str,
    ) -> list[LegislatorVote]:
        """
        Extract a legislator's votes from a bill's vote records.

        Args:
            bill: OpenStates bill dict with votes included
            person_id: OpenStates person ID to filter for

        Returns:
            List of LegislatorVote objects for this legislator
        """
        votes_list: list[LegislatorVote] = []

        bill_identifier = bill.get("identifier", "")
        bill_title = bill.get("title", "Unknown Bill")

        # Try to find DDP link for this bill
        ddp_url, _ = self._find_bill_ddp_link(bill)

        # Process each vote event on this bill
        for vote_event in bill.get("votes", []):
            motion_text = vote_event.get("motion_text", "")
            vote_date = vote_event.get("start_date", "")
            vote_result = vote_event.get("result", "")

            # Get chamber from organization
            org = vote_event.get("organization", {})
            chamber = ""
            if isinstance(org, dict):
                org_classification = org.get("classification", "")
                if org_classification in ("upper", "lower"):
                    chamber = org_classification
                else:
                    # Try to infer from name
                    org_name = org.get("name", "").lower()
                    if "senate" in org_name:
                        chamber = "upper"
                    elif "house" in org_name or "assembly" in org_name:
                        chamber = "lower"

            # Check individual votes for this legislator
            for individual_vote in vote_event.get("votes", []):
                voter_id = individual_vote.get("voter_id", "")
                if voter_id == person_id:
                    vote_option = individual_vote.get("option", "").lower()

                    votes_list.append(
                        LegislatorVote(
                            bill_identifier=bill_identifier,
                            bill_title=bill_title,
                            vote_option=vote_option,
                            vote_date=vote_date,
                            motion_text=motion_text,
                            vote_result=vote_result,
                            chamber=chamber,
                            bill_ddp_url=ddp_url,
                        )
                    )

        return votes_list

    def format_legislator_votes_chunk(
        self,
        legislator: dict,
        votes: list[LegislatorVote],
    ) -> str:
        """
        Format a legislator's voting record for RAG.

        Args:
            legislator: Legislator dict with name, party, chamber, etc.
            votes: List of LegislatorVote objects

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
            parts.append(f"## Voting Record for [{name}]({ddp_url})")
        else:
            parts.append(f"## Voting Record for {name}")

        # Basic info line
        info_parts = []
        if party:
            info_parts.append(f"**Party:** {party}")
        if chamber:
            chamber_display = "Senate" if chamber.lower() in ("upper", "senate") else "House"
            info_parts.append(f"**Chamber:** {chamber_display}")
        if jurisdiction:
            info_parts.append(f"**State:** {jurisdiction.upper()}")
        if info_parts:
            parts.append(" | ".join(info_parts))

        if not votes:
            parts.append("\nNo recorded votes found in the current legislative session.")
            return "\n".join(parts)

        # Vote statistics
        yes_votes = [v for v in votes if v.vote_option == "yes"]
        no_votes = [v for v in votes if v.vote_option == "no"]
        other_votes = [v for v in votes if v.vote_option not in ("yes", "no")]

        parts.append(f"\n### Voting Summary")
        parts.append(f"- **Total Votes Cast:** {len(votes)}")
        parts.append(f"- **Yes Votes:** {len(yes_votes)}")
        parts.append(f"- **No Votes:** {len(no_votes)}")
        if other_votes:
            parts.append(f"- **Other (abstain/excused/not voting):** {len(other_votes)}")

        # Group votes by option for detailed listing
        # Sort by date (most recent first)
        sorted_votes = sorted(votes, key=lambda v: v.vote_date, reverse=True)

        # Yes votes
        if yes_votes:
            parts.append(f"\n### Bills Voted YES ({len(yes_votes)})")
            for vote in sorted_votes:
                if vote.vote_option == "yes":
                    formatted = self._format_vote_line(vote)
                    parts.append(f"- {formatted}")
                    if len([p for p in parts if p.startswith("- ")]) > 50:
                        remaining = len([v for v in sorted_votes if v.vote_option == "yes"]) - 50
                        if remaining > 0:
                            parts.append(f"  ...and {remaining} more YES votes")
                        break

        # No votes
        if no_votes:
            parts.append(f"\n### Bills Voted NO ({len(no_votes)})")
            count = 0
            for vote in sorted_votes:
                if vote.vote_option == "no":
                    formatted = self._format_vote_line(vote)
                    parts.append(f"- {formatted}")
                    count += 1
                    if count >= 50:
                        remaining = len(no_votes) - 50
                        if remaining > 0:
                            parts.append(f"  ...and {remaining} more NO votes")
                        break

        # Other votes (limited)
        if other_votes:
            parts.append(f"\n### Other Votes ({len(other_votes)})")
            count = 0
            for vote in sorted_votes:
                if vote.vote_option not in ("yes", "no"):
                    formatted = self._format_vote_line(vote, include_option=True)
                    parts.append(f"- {formatted}")
                    count += 1
                    if count >= 20:
                        remaining = len(other_votes) - 20
                        if remaining > 0:
                            parts.append(f"  ...and {remaining} more")
                        break

        return "\n".join(parts)

    def _format_vote_line(self, vote: LegislatorVote, include_option: bool = False) -> str:
        """
        Format a single vote as a line item.

        Args:
            vote: LegislatorVote object
            include_option: Whether to include the vote option in output

        Returns:
            Formatted vote line
        """
        title = vote.bill_title
        # Truncate long titles
        if len(title) > 70:
            title = title[:67] + "..."

        if vote.bill_ddp_url:
            line = f"[{title}]({vote.bill_ddp_url})"
        else:
            line = title

        line += f" ({vote.bill_identifier})"

        if vote.vote_date:
            line += f" - {vote.vote_date}"

        if include_option:
            line += f" [{vote.vote_option}]"

        return line

    async def sync_legislator_votes(
        self,
        legislator: dict,
        session: str | None = None,
        max_bills: int = 200,
    ) -> LegislatorSyncResult:
        """
        Sync a single legislator's voting record.

        Args:
            legislator: Legislator dict with openstates_id, name, slug, etc.
            session: Optional session filter
            max_bills: Maximum bills to check for votes

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

        # Fetch votes
        votes = await self.fetch_legislator_votes(
            person_id=openstates_id,
            jurisdiction=jurisdiction,
            session=session,
            max_bills=max_bills,
        )

        # Format the content chunk
        content = self.format_legislator_votes_chunk(legislator, votes)

        # Create document metadata
        source_name = self._get_source_name(jurisdiction)
        metadata = DocumentMetadata(
            document_id=f"legislator-votes-{openstates_id}",
            document_type="legislator-votes",
            source=source_name,
            title=f"{name} - Voting Record",
            jurisdiction=jurisdiction.upper(),
            legislator_id=openstates_id,
            extra={
                "slug": legislator.get("slug", ""),
                "party": legislator.get("party", ""),
                "chamber": legislator.get("chamber", ""),
                "votes_count": len(votes),
                "yes_votes": len([v for v in votes if v.vote_option == "yes"]),
                "no_votes": len([v for v in votes if v.vote_option == "no"]),
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
                votes_found=len(votes),
                chunks_created=result.chunks_created,
            )

        except Exception as e:
            logger.error(
                "Failed to ingest legislator votes",
                legislator_id=openstates_id,
                error=str(e),
            )
            return LegislatorSyncResult(
                legislator_id=openstates_id,
                legislator_name=name,
                success=False,
                error=str(e),
            )

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
        include_votes: bool = False,
        vote_session: str | None = None,
        max_vote_bills: int = 200,
    ) -> LegislatorSyncResult:
        """
        Sync a single legislator's sponsored bills and optionally voting record.

        Args:
            legislator: Legislator dict with openstates_id, name, slug, etc.
            include_votes: Whether to also sync voting record
            vote_session: Optional session filter for votes
            max_vote_bills: Maximum bills to check for votes

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

        total_chunks = 0
        bills_found = 0
        votes_found = 0
        errors: list[str] = []

        # Fetch and sync sponsored bills
        bills = await self.fetch_sponsored_bills(
            person_id=openstates_id,
            jurisdiction=jurisdiction,
        )

        if not bills:
            logger.debug(f"No sponsored bills found for {name}")
            # Still create a document indicating no bills found
            # This is useful for RAG to know we checked

        bills_found = len(bills)

        # Format the bills content chunk
        bills_content = self.format_legislator_bills_chunk(legislator, bills)

        # Create bills document metadata
        source_name = self._get_source_name(jurisdiction)
        bills_metadata = DocumentMetadata(
            document_id=f"legislator-bills-{openstates_id}",
            document_type="legislator-bills",
            source=source_name,
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

        # Ingest the bills document
        try:
            result = await self.pipeline.ingest_document(
                content=bills_content,
                metadata=bills_metadata,
                skip_duplicates=False,  # Always update
            )
            total_chunks += result.chunks_created

        except Exception as e:
            logger.error(
                "Failed to ingest legislator bills",
                legislator_id=openstates_id,
                error=str(e),
            )
            errors.append(f"Bills sync failed: {e}")

        # Sync voting record if requested
        if include_votes:
            votes_result = await self.sync_legislator_votes(
                legislator=legislator,
                session=vote_session,
                max_bills=max_vote_bills,
            )
            if votes_result.success:
                total_chunks += votes_result.chunks_created
                votes_found = votes_result.votes_found
            else:
                errors.append(f"Votes sync failed: {votes_result.error}")

        success = len(errors) == 0 or total_chunks > 0

        return LegislatorSyncResult(
            legislator_id=openstates_id,
            legislator_name=name,
            success=success,
            bills_found=bills_found,
            votes_found=votes_found,
            chunks_created=total_chunks,
            error="; ".join(errors) if errors else None,
        )

    async def sync_all_legislators(
        self,
        legislators: list[dict],
        include_votes: bool = False,
        vote_session: str | None = None,
        max_vote_bills: int = 200,
    ) -> SyncBatchResult:
        """
        Sync sponsored bills and optionally voting records for all legislators.

        Args:
            legislators: List of legislator dicts from Webflow with:
                - openstates_id: OpenStates person ID
                - name: Legislator name
                - slug: Webflow slug for DDP URL
                - jurisdiction: State code
                - party: Political party
                - chamber: Legislative chamber
            include_votes: Whether to also sync voting records
            vote_session: Optional session filter for votes
            max_vote_bills: Maximum bills to check for votes per legislator

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
        total_votes = 0
        chunks_created = 0
        errors: list[str] = []
        start_time = time.time()

        sync_type = "bills + votes" if include_votes else "bills only"
        logger.info(
            "Starting legislator sync",
            total_legislators=total,
            sync_type=sync_type,
            rate_limit_rpm=self.rate_limit.requests_per_minute,
        )

        for i, legislator in enumerate(legislators):
            name = legislator.get("name", "Unknown")

            result = await self.sync_legislator(
                legislator,
                include_votes=include_votes,
                vote_session=vote_session,
                max_vote_bills=max_vote_bills,
            )

            if result.success:
                successful += 1
                total_bills += result.bills_found
                total_votes += result.votes_found
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
                remaining = total - processed
                eta_seconds = remaining / rate if rate > 0 else 0
                eta_minutes = int(eta_seconds / 60)

                logger.info(
                    "Legislator sync progress",
                    processed=processed,
                    total=total,
                    successful=successful,
                    failed=failed,
                    total_bills=total_bills,
                    total_votes=total_votes,
                    rate_per_second=round(rate, 2),
                    eta_minutes=eta_minutes,
                )

        # Final stats
        elapsed = time.time() - start_time
        elapsed_minutes = int(elapsed / 60)
        elapsed_seconds = int(elapsed % 60)

        logger.info(
            "Legislator sync complete",
            total_time=f"{elapsed_minutes}m {elapsed_seconds}s",
            successful=successful,
            failed=failed,
            total_bills=total_bills,
            total_votes=total_votes,
            chunks_created=chunks_created,
        )

        return SyncBatchResult(
            total_legislators=total,
            successful=successful,
            failed=failed,
            total_bills_found=total_bills,
            total_votes_found=total_votes,
            chunks_created=chunks_created,
            errors=errors,
        )
