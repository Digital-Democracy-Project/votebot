"""Bill votes lookup service with Pinecone caching.

This service provides real-time vote lookup for bills that aren't in our system,
with automatic caching to Pinecone for future queries.
"""

import re
from dataclasses import dataclass
from datetime import date

import httpx
import structlog

from votebot.config import Settings, get_settings
from votebot.ingestion.metadata import DocumentMetadata
from votebot.ingestion.pipeline import IngestionPipeline
from votebot.services.vector_store import VectorStoreService

logger = structlog.get_logger()


@dataclass
class VoteRecord:
    """Individual vote record for a legislator."""

    legislator_id: str
    legislator_name: str
    vote: str  # yes, no, abstain, not_voting, etc.
    party: str | None = None


@dataclass
class BillVote:
    """A single vote event on a bill."""

    vote_id: str
    motion_text: str
    result: str  # passed, failed
    date: str
    chamber: str  # house, senate
    yes_count: int
    no_count: int
    other_count: int
    votes: list[VoteRecord]


@dataclass
class BillVotesResult:
    """Result of fetching bill votes."""

    bill_id: str
    bill_identifier: str  # e.g., "HB 1234"
    jurisdiction: str
    title: str | None
    votes: list[BillVote]
    cached: bool = False  # Whether this came from cache
    openstates_id: str | None = None


class BillVotesService:
    """
    Service for fetching and caching bill votes.

    This service:
    - Checks Pinecone first for cached vote data
    - Falls back to OpenStates API if not cached
    - Stores fetched votes in Pinecone for future use
    """

    OPENSTATES_API_BASE = "https://v3.openstates.org"

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the bill votes service.

        Args:
            settings: Application settings
        """
        self.settings = settings or get_settings()
        self.api_key = self.settings.openstates_api_key.get_secret_value()
        self.pipeline = IngestionPipeline(self.settings)
        self.vector_store = VectorStoreService(self.settings)
        # Cache for legislator lookups
        self._legislator_cache: dict[str, dict] = {}

    async def get_bill_votes(
        self,
        jurisdiction: str,
        session: str,
        bill_identifier: str,
    ) -> BillVotesResult | None:
        """
        Get votes for a bill, checking cache first.

        Args:
            jurisdiction: State code (e.g., 'fl', 'us')
            session: Session identifier (e.g., '2025', '119')
            bill_identifier: Bill identifier (e.g., 'HB 1234')

        Returns:
            BillVotesResult or None if not found
        """
        # Normalize the bill identifier
        clean_bill_id = bill_identifier.replace(" ", "")
        cache_key = f"bill-votes-{jurisdiction}-{session}-{clean_bill_id}".lower()

        logger.info(
            "Looking up bill votes",
            jurisdiction=jurisdiction,
            session=session,
            bill_identifier=bill_identifier,
            cache_key=cache_key,
        )

        # Step 1: Check Pinecone cache
        cached_result = await self._check_cache(cache_key)
        if cached_result:
            logger.info("Found cached bill votes", cache_key=cache_key)
            return cached_result

        # Step 2: Fetch from OpenStates
        logger.info("Fetching bill votes from OpenStates", bill_identifier=bill_identifier)
        result = await self._fetch_from_openstates(jurisdiction, session, bill_identifier)

        if result:
            # Step 3: Cache to Pinecone
            await self._cache_to_pinecone(result, cache_key)
            logger.info(
                "Cached bill votes to Pinecone",
                cache_key=cache_key,
                vote_count=len(result.votes),
            )

        return result

    async def get_bill_votes_by_url(self, openstates_url: str) -> BillVotesResult | None:
        """
        Get votes for a bill using its OpenStates URL.

        Args:
            openstates_url: URL like https://openstates.org/fl/bills/2025/HB1234/

        Returns:
            BillVotesResult or None if not found
        """
        # Parse the URL
        pattern = r"https?://openstates\.org/([a-z]{2})/bills/([^/]+)/([^/]+)/?"
        match = re.match(pattern, openstates_url, re.IGNORECASE)
        if not match:
            logger.warning("Invalid OpenStates URL", url=openstates_url)
            return None

        jurisdiction = match.group(1).lower()
        session = match.group(2)
        bill_id = match.group(3)

        return await self.get_bill_votes(jurisdiction, session, bill_id)

    async def _check_cache(self, cache_key: str) -> BillVotesResult | None:
        """Check Pinecone for cached vote data."""
        try:
            # Query by document_id metadata filter
            results = await self.vector_store.query(
                query_text=f"bill votes {cache_key}",
                top_k=1,
                filter={"document_id": cache_key},
            )

            if results and len(results) > 0:
                # Found cached data - extract the structured data
                chunk = results[0]
                metadata = chunk.get("metadata", {})

                # The votes are stored as formatted text, parse basic info
                return BillVotesResult(
                    bill_id=cache_key,
                    bill_identifier=metadata.get("bill_id", ""),
                    jurisdiction=metadata.get("jurisdiction", ""),
                    title=metadata.get("title", ""),
                    votes=[],  # Full vote details are in the text content
                    cached=True,
                    openstates_id=metadata.get("openstates_id"),
                )

        except Exception as e:
            logger.warning("Error checking vote cache", error=str(e))

        return None

    async def _fetch_from_openstates(
        self,
        jurisdiction: str,
        session: str,
        bill_identifier: str,
    ) -> BillVotesResult | None:
        """Fetch bill and votes from OpenStates API (supports state and federal bills)."""
        clean_bill_id = bill_identifier.replace(" ", "")

        # OpenStates now supports federal bills (after Plural Policy acquisition)
        # Federal jurisdiction uses 'us' and Congress number as session (e.g., '119')
        url = f"{self.OPENSTATES_API_BASE}/bills/{jurisdiction.lower()}/{session}/{clean_bill_id}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                headers = {
                    "accept": "application/json",
                    "x-api-key": self.api_key,
                }
                # Include votes in the response
                params = [("include", "votes"), ("include", "sponsorships")]
                response = await client.get(url, headers=headers, params=params)

                if response.status_code == 404:
                    logger.warning(
                        "Bill not found in OpenStates",
                        jurisdiction=jurisdiction,
                        session=session,
                        bill_identifier=bill_identifier,
                    )
                    return None

                response.raise_for_status()
                data = response.json()

                # Parse the votes
                votes = self._parse_votes(data.get("votes", []))

                return BillVotesResult(
                    bill_id=f"{jurisdiction}-{session}-{clean_bill_id}",
                    bill_identifier=data.get("identifier", bill_identifier),
                    jurisdiction=jurisdiction.upper(),
                    title=data.get("title"),
                    votes=votes,
                    cached=False,
                    openstates_id=data.get("id"),
                )

        except httpx.TimeoutException:
            logger.error("Timeout fetching from OpenStates", bill_identifier=bill_identifier)
            return None
        except Exception as e:
            logger.error("Error fetching from OpenStates", error=str(e))
            return None

    async def _fetch_from_congress_gov(
        self,
        session: str,
        bill_identifier: str,
    ) -> BillVotesResult | None:
        """Fetch bill and votes from Congress.gov API."""
        # Parse bill type and number from identifier (e.g., "HR1", "S 123", "HJRES45")
        match = re.match(r"([A-Z]+)\s*(\d+)", bill_identifier.upper())
        if not match:
            logger.warning("Could not parse federal bill identifier", bill_identifier=bill_identifier)
            return None

        bill_type_raw = match.group(1)
        bill_number = match.group(2)

        # Map common bill type abbreviations to Congress.gov format
        type_map = {
            "HR": "hr",
            "H": "hr",
            "S": "s",
            "HRES": "hres",
            "SRES": "sres",
            "HJRES": "hjres",
            "SJRES": "sjres",
            "HCONRES": "hconres",
            "SCONRES": "sconres",
        }
        bill_type = type_map.get(bill_type_raw, bill_type_raw.lower())

        # Determine Congress number from session (e.g., "2025" -> 119th Congress)
        try:
            year = int(session)
            congress = ((year - 1789) // 2) + 1
        except ValueError:
            # Maybe session is already congress number
            try:
                congress = int(session)
            except ValueError:
                congress = 119  # Default to current Congress

        api_key = self.settings.congress_api_key.get_secret_value() if self.settings.congress_api_key else ""

        # First, get the bill details
        bill_url = f"https://api.congress.gov/v3/bill/{congress}/{bill_type}/{bill_number}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                params = {"api_key": api_key, "format": "json"}

                # Get bill info
                response = await client.get(bill_url, params=params)
                if response.status_code == 404:
                    logger.warning(
                        "Bill not found in Congress.gov",
                        congress=congress,
                        bill_type=bill_type,
                        bill_number=bill_number,
                    )
                    return None

                response.raise_for_status()
                bill_data = response.json().get("bill", {})

                title = bill_data.get("title", "")
                bill_id = f"{bill_type.upper()}{bill_number}"

                # Get actions which may include roll call votes
                actions_url = f"{bill_url}/actions"
                actions_response = await client.get(actions_url, params=params)
                actions_data = actions_response.json() if actions_response.status_code == 200 else {}

                # Parse votes from actions
                votes = await self._parse_congress_votes(
                    actions_data.get("actions", []),
                    congress,
                    client,
                    params,
                )

                logger.info(
                    "Congress.gov bill lookup complete",
                    bill_id=bill_id,
                    title=title[:50] if title else None,
                    vote_count=len(votes),
                )

                return BillVotesResult(
                    bill_id=f"us-{congress}-{bill_id}",
                    bill_identifier=bill_id,
                    jurisdiction="US",
                    title=title,
                    votes=votes,
                    cached=False,
                    openstates_id=None,
                )

        except httpx.TimeoutException:
            logger.error("Timeout fetching from Congress.gov", bill_identifier=bill_identifier)
            return None
        except Exception as e:
            logger.error("Error fetching from Congress.gov", error=str(e))
            return None

    async def _parse_congress_votes(
        self,
        actions: list,
        congress: int,
        client: httpx.AsyncClient,
        params: dict,
    ) -> list[BillVote]:
        """Parse votes from Congress.gov bill actions."""
        votes = []

        for action in actions:
            # Look for roll call vote references in action text
            action_text = action.get("text", "")
            recorded_votes = action.get("recordedVotes", [])

            for rv in recorded_votes:
                chamber = rv.get("chamber", "").lower()
                roll_num = rv.get("rollNumber")
                date = rv.get("date", action.get("actionDate", ""))
                url = rv.get("url", "")

                # Try to fetch detailed roll call vote
                vote_details = await self._fetch_roll_call_vote(
                    congress, chamber, roll_num, client, params
                )

                if vote_details:
                    votes.append(vote_details)
                else:
                    # Fallback to basic info from action
                    votes.append(BillVote(
                        vote_id=f"{chamber}-{congress}-{roll_num}" if roll_num else "",
                        motion_text=action_text[:200] if action_text else "Roll Call Vote",
                        result="recorded",
                        date=date,
                        chamber=chamber,
                        yes_count=0,
                        no_count=0,
                        other_count=0,
                        votes=[],
                    ))

        return votes

    async def _fetch_roll_call_vote(
        self,
        congress: int,
        chamber: str,
        roll_number: int | None,
        client: httpx.AsyncClient,
        params: dict,
    ) -> BillVote | None:
        """Fetch detailed roll call vote from Congress.gov."""
        if not roll_number:
            return None

        # Map chamber names
        chamber_map = {"house": "house", "senate": "senate", "h": "house", "s": "senate"}
        chamber_normalized = chamber_map.get(chamber.lower(), chamber.lower())

        try:
            url = f"https://api.congress.gov/v3/{chamber_normalized}/vote/{congress}/{roll_number}"
            response = await client.get(url, params=params)

            if response.status_code != 200:
                return None

            data = response.json().get("vote", {})

            # Parse vote counts
            yes_count = data.get("yeas", 0) or data.get("yea", {}).get("total", 0)
            no_count = data.get("nays", 0) or data.get("nay", {}).get("total", 0)
            other_count = (
                data.get("present", 0) +
                data.get("notVoting", 0)
            )

            # Parse individual votes if available
            vote_records = []
            for position in ["yea", "nay", "present", "notVoting"]:
                pos_data = data.get(position, {})
                members = pos_data.get("members", []) if isinstance(pos_data, dict) else []
                for member in members:
                    vote_records.append(VoteRecord(
                        legislator_id=member.get("bioguideId", ""),
                        legislator_name=f"{member.get('firstName', '')} {member.get('lastName', '')}".strip(),
                        vote=position.lower().replace("notvoting", "not_voting"),
                        party=member.get("party"),
                    ))

            return BillVote(
                vote_id=f"{chamber_normalized}-{congress}-{roll_number}",
                motion_text=data.get("question", "Roll Call Vote"),
                result="passed" if data.get("result", "").lower() in ["passed", "agreed to"] else "failed",
                date=data.get("date", ""),
                chamber=chamber_normalized,
                yes_count=yes_count,
                no_count=no_count,
                other_count=other_count,
                votes=vote_records,
            )

        except Exception as e:
            logger.warning("Error fetching roll call vote", error=str(e))
            return None

    def _parse_votes(self, votes_data: list) -> list[BillVote]:
        """Parse votes from OpenStates response."""
        votes = []
        for vote in votes_data:
            # Parse counts
            counts = vote.get("counts", [])
            yes_count = next((c.get("value", 0) for c in counts if c.get("option") == "yes"), 0)
            no_count = next((c.get("value", 0) for c in counts if c.get("option") == "no"), 0)
            other_count = sum(
                c.get("value", 0) for c in counts
                if c.get("option") not in ("yes", "no")
            )

            # Parse individual votes
            vote_records = []
            for v in vote.get("votes", []):
                vote_records.append(VoteRecord(
                    legislator_id=v.get("legislator_id", ""),
                    legislator_name=v.get("voter_name", "Unknown"),
                    vote=v.get("option", "unknown"),
                    party=v.get("party"),
                ))

            # Get chamber from organization
            org = vote.get("organization", {})
            chamber = org.get("classification", "") if isinstance(org, dict) else ""

            votes.append(BillVote(
                vote_id=vote.get("id", ""),
                motion_text=vote.get("motion_text", "Vote"),
                result=vote.get("result", "unknown"),
                date=vote.get("start_date", ""),
                chamber=chamber,
                yes_count=yes_count,
                no_count=no_count,
                other_count=other_count,
                votes=vote_records,
            ))

        return votes

    async def _cache_to_pinecone(self, result: BillVotesResult, cache_key: str) -> None:
        """Cache vote data to Pinecone."""
        # Format votes as searchable text
        content = self.format_votes_document(result)

        # Create metadata
        metadata = DocumentMetadata(
            document_id=cache_key,
            document_type="bill-votes",
            source="OpenStates",
            title=f"{result.bill_identifier} - Voting Record",
            jurisdiction=result.jurisdiction,
            bill_id=result.bill_identifier,
            extra={
                "openstates_id": result.openstates_id,
                "vote_count": len(result.votes),
                "cached_date": date.today().isoformat(),
            },
        )

        # Ingest to Pinecone
        try:
            await self.pipeline.ingest_document(
                content=content,
                metadata=metadata,
                skip_duplicates=False,
            )
        except Exception as e:
            logger.error("Failed to cache votes to Pinecone", error=str(e))

    def format_votes_document(self, result: BillVotesResult) -> str:
        """Format votes into a searchable document."""
        parts = []

        # Header
        parts.append(f"## Voting Record: {result.bill_identifier}")
        if result.title:
            parts.append(f"**Title:** {result.title}")
        parts.append(f"**Jurisdiction:** {result.jurisdiction}")

        if not result.votes:
            parts.append("\nNo recorded votes for this bill.")
            return "\n".join(parts)

        # Each vote event
        for vote in result.votes:
            parts.append(f"\n### {vote.chamber.title()} Vote - {vote.date}")
            parts.append(f"**Motion:** {vote.motion_text}")
            parts.append(f"**Result:** {vote.result.upper()} (Yes: {vote.yes_count}, No: {vote.no_count}, Other: {vote.other_count})")

            # Group voters by their vote
            yes_voters = [v for v in vote.votes if v.vote.lower() == "yes"]
            no_voters = [v for v in vote.votes if v.vote.lower() == "no"]
            other_voters = [v for v in vote.votes if v.vote.lower() not in ("yes", "no")]

            if yes_voters:
                names = [v.legislator_name for v in yes_voters[:30]]
                if len(yes_voters) > 30:
                    names.append(f"and {len(yes_voters) - 30} others")
                parts.append(f"**Voted Yes:** {', '.join(names)}")

            if no_voters:
                names = [v.legislator_name for v in no_voters[:30]]
                if len(no_voters) > 30:
                    names.append(f"and {len(no_voters) - 30} others")
                parts.append(f"**Voted No:** {', '.join(names)}")

            if other_voters:
                other_grouped: dict[str, list[str]] = {}
                for v in other_voters:
                    opt = v.vote.lower()
                    if opt not in other_grouped:
                        other_grouped[opt] = []
                    other_grouped[opt].append(v.legislator_name)

                for opt, names in other_grouped.items():
                    display_names = names[:10]
                    if len(names) > 10:
                        display_names.append(f"and {len(names) - 10} others")
                    parts.append(f"**{opt.replace('_', ' ').title()}:** {', '.join(display_names)}")

        return "\n".join(parts)

    async def lookup_legislator_vote(
        self,
        legislator_name: str,
        jurisdiction: str,
        session: str,
        bill_identifier: str,
    ) -> dict | None:
        """
        Look up how a specific legislator voted on a bill.

        Args:
            legislator_name: Name of the legislator
            jurisdiction: State code
            session: Session identifier
            bill_identifier: Bill identifier

        Returns:
            Dict with vote info or None if not found
        """
        result = await self.get_bill_votes(jurisdiction, session, bill_identifier)
        if not result:
            return None

        # Search for the legislator in all votes
        legislator_lower = legislator_name.lower()
        for vote in result.votes:
            for record in vote.votes:
                if legislator_lower in record.legislator_name.lower():
                    return {
                        "legislator": record.legislator_name,
                        "vote": record.vote,
                        "motion": vote.motion_text,
                        "date": vote.date,
                        "chamber": vote.chamber,
                        "result": vote.result,
                        "bill": result.bill_identifier,
                    }

        return None
