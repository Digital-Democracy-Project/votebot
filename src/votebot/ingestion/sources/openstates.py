"""OpenStates API data source connector."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator

import httpx
import structlog

from votebot.config import Settings, get_settings
from votebot.ingestion.metadata import DocumentMetadata, MetadataExtractor
from votebot.ingestion.pipeline import DocumentSource

logger = structlog.get_logger()


@dataclass
class LegislativeSession:
    """Represents a legislative session from OpenStates."""

    identifier: str
    name: str
    classification: str  # "primary" or "special"
    start_date: str
    end_date: str

    def is_current(self) -> bool:
        """Check if this session is currently active."""
        today = datetime.now().date()
        try:
            start = datetime.strptime(self.start_date, "%Y-%m-%d").date()
            end = datetime.strptime(self.end_date, "%Y-%m-%d").date()
            return start <= today <= end
        except (ValueError, TypeError):
            return False

    def is_primary(self) -> bool:
        """Check if this is a primary (regular) session."""
        return self.classification == "primary"


@dataclass
class JurisdictionInfo:
    """
    Jurisdiction metadata from OpenStates API.

    Contains legislative sessions, organizations (chambers), and data freshness info.
    """

    id: str
    name: str
    classification: str  # "state" or "country"
    url: str
    latest_bill_update: datetime | None = None
    latest_people_update: datetime | None = None
    sessions: list[LegislativeSession] = field(default_factory=list)
    organizations: list[dict[str, Any]] = field(default_factory=list)

    def get_current_session(self) -> LegislativeSession | None:
        """Get the currently active session, preferring primary sessions."""
        # First try to find an active primary session
        for session in self.sessions:
            if session.is_current() and session.is_primary():
                return session

        # Fall back to any active session (including special)
        for session in self.sessions:
            if session.is_current():
                return session

        # If no active session, return the most recent primary session
        primary_sessions = [s for s in self.sessions if s.is_primary()]
        if primary_sessions:
            # Sort by start_date descending
            primary_sessions.sort(key=lambda s: s.start_date, reverse=True)
            return primary_sessions[0]

        return None

    def get_session_by_identifier(self, identifier: str) -> LegislativeSession | None:
        """Get a session by its identifier (e.g., '2026', '2025A')."""
        for session in self.sessions:
            if session.identifier == identifier:
                return session
        return None

    def needs_bill_sync(self, last_sync: datetime | None) -> bool:
        """Check if bills need to be synced based on OpenStates update time."""
        if not last_sync or not self.latest_bill_update:
            return True
        return self.latest_bill_update > last_sync

    def needs_people_sync(self, last_sync: datetime | None) -> bool:
        """Check if legislators need to be synced based on OpenStates update time."""
        if not last_sync or not self.latest_people_update:
            return True
        return self.latest_people_update > last_sync


class OpenStatesSource:
    """
    Data source connector for OpenStates API.

    Fetches state legislature data:
    - Bills
    - Votes
    - Legislators
    """

    BASE_URL = "https://v3.openstates.org"

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

    def __init__(
        self,
        settings: Settings | None = None,
        metadata_extractor: MetadataExtractor | None = None,
    ):
        """
        Initialize the OpenStates source.

        Args:
            settings: Application settings
            metadata_extractor: Metadata extractor instance
        """
        self.settings = settings or get_settings()
        self.metadata_extractor = metadata_extractor or MetadataExtractor()
        self.api_key = self.settings.openstates_api_key.get_secret_value()
        # Cache for legislator lookups (openstates_id -> {name, slug})
        self._legislator_cache: dict[str, dict] = {}

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

    async def fetch_jurisdiction(
        self,
        jurisdiction: str,
        max_retries: int = 3,
    ) -> JurisdictionInfo | None:
        """
        Fetch jurisdiction metadata from OpenStates API.

        Includes legislative sessions, organizations (chambers/committees),
        and data freshness timestamps.

        Args:
            jurisdiction: State abbreviation (e.g., 'fl', 'wa') or 'us' for federal
            max_retries: Maximum retries for rate limit errors

        Returns:
            JurisdictionInfo or None if not found
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = {
                "accept": "application/json",
                "X-API-Key": self.api_key,
            }

            # Include all available data
            include_params = [
                "organizations",
                "legislative_sessions",
                "latest_runs",
            ]
            params = [("include", p) for p in include_params]

            url = f"{self.BASE_URL}/jurisdictions/{jurisdiction.lower()}"

            logger.debug(
                "Fetching jurisdiction from OpenStates",
                jurisdiction=jurisdiction,
            )

            for attempt in range(max_retries):
                try:
                    response = await client.get(url, headers=headers, params=params)

                    if response.status_code == 429:
                        retry_after = int(response.headers.get("Retry-After", 2))
                        logger.warning(
                            f"Rate limited, waiting {retry_after}s (attempt {attempt + 1}/{max_retries})",
                            jurisdiction=jurisdiction,
                        )
                        await asyncio.sleep(retry_after)
                        continue

                    if response.status_code == 404:
                        logger.warning("Jurisdiction not found", jurisdiction=jurisdiction)
                        return None

                    response.raise_for_status()
                    data = response.json()
                    break

                except httpx.HTTPStatusError as e:
                    logger.error(
                        "Failed to fetch jurisdiction",
                        jurisdiction=jurisdiction,
                        status_code=e.response.status_code,
                    )
                    return None
                except Exception as e:
                    logger.error(
                        "Failed to fetch jurisdiction",
                        jurisdiction=jurisdiction,
                        error=str(e),
                    )
                    return None
            else:
                logger.error("Exhausted retries fetching jurisdiction", jurisdiction=jurisdiction)
                return None

            # Parse sessions
            sessions = []
            for session_data in data.get("legislative_sessions", []):
                sessions.append(
                    LegislativeSession(
                        identifier=session_data.get("identifier", ""),
                        name=session_data.get("name", ""),
                        classification=session_data.get("classification", ""),
                        start_date=session_data.get("start_date", ""),
                        end_date=session_data.get("end_date", ""),
                    )
                )

            # Parse timestamps
            latest_bill_update = None
            if data.get("latest_bill_update"):
                try:
                    latest_bill_update = datetime.fromisoformat(
                        data["latest_bill_update"].replace("Z", "+00:00")
                    )
                except (ValueError, TypeError):
                    pass

            latest_people_update = None
            if data.get("latest_people_update"):
                try:
                    latest_people_update = datetime.fromisoformat(
                        data["latest_people_update"].replace("Z", "+00:00")
                    )
                except (ValueError, TypeError):
                    pass

            return JurisdictionInfo(
                id=data.get("id", ""),
                name=data.get("name", ""),
                classification=data.get("classification", ""),
                url=data.get("url", ""),
                latest_bill_update=latest_bill_update,
                latest_people_update=latest_people_update,
                sessions=sessions,
                organizations=data.get("organizations", []),
            )

    async def get_current_session_identifier(self, jurisdiction: str) -> str | None:
        """
        Get the current session identifier for a jurisdiction.

        Args:
            jurisdiction: State abbreviation (e.g., 'fl', 'wa')

        Returns:
            Session identifier (e.g., '2026', '119') or None
        """
        info = await self.fetch_jurisdiction(jurisdiction)
        if info:
            session = info.get_current_session()
            if session:
                return session.identifier
        return None

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
            logger.debug("No legislators collection ID configured, skipping legislator mapping")
            return

        webflow_api_key = self.settings.webflow_votebot_api_key.get_secret_value()
        if not webflow_api_key:
            logger.debug("No Webflow API key configured, skipping legislator mapping")
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

    async def fetch(
        self,
        jurisdiction: str | None = None,
        session: str | None = None,
        limit: int = 100,
        **kwargs,
    ) -> AsyncIterator[DocumentSource]:
        """
        Fetch bills from OpenStates.

        Args:
            jurisdiction: State abbreviation (e.g., 'ca', 'ny')
            session: Legislative session
            limit: Maximum number of bills to fetch

        Yields:
            DocumentSource objects for each bill
        """
        # Build legislator mapping for sponsor DDP links
        await self._build_legislator_mapping()

        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = {"X-API-Key": self.api_key}

            # Build query parameters
            params = {
                "per_page": min(limit, 50),  # API max per page
            }
            if jurisdiction:
                params["jurisdiction"] = jurisdiction
            if session:
                params["session"] = session

            logger.info(
                "Fetching bills from OpenStates",
                jurisdiction=jurisdiction,
                session=session,
                limit=limit,
            )

            try:
                response = await client.get(
                    f"{self.BASE_URL}/bills",
                    params=params,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
            except Exception as e:
                logger.error("Failed to fetch from OpenStates", error=str(e))
                return

            bills = data.get("results", [])
            logger.info(f"Found {len(bills)} bills")

            for bill in bills:
                try:
                    # Fetch full bill details
                    bill_id = bill.get("id")
                    if bill_id:
                        detail_response = await client.get(
                            f"{self.BASE_URL}/bills/{bill_id}",
                            headers=headers,
                        )
                        detail_response.raise_for_status()
                        bill_detail = detail_response.json()
                    else:
                        bill_detail = bill

                    # Extract content
                    content = self._extract_bill_content(bill_detail)
                    if not content:
                        continue

                    # Get jurisdiction from bill detail or function parameter
                    bill_jurisdiction = bill_detail.get("jurisdiction", {}).get("id", jurisdiction)
                    source_name = self._get_source_name(bill_jurisdiction)

                    # Extract metadata
                    metadata = self.metadata_extractor.extract_bill_metadata(
                        bill_detail,
                        source=source_name,
                    )

                    yield DocumentSource(
                        content=content,
                        metadata=metadata,
                    )

                except Exception as e:
                    logger.warning(
                        "Failed to process bill",
                        bill=bill.get("identifier"),
                        error=str(e),
                    )
                    continue

    async def fetch_bill(
        self,
        bill_id: str,
    ) -> DocumentSource | None:
        """
        Fetch a specific bill by OpenStates ID.

        Args:
            bill_id: OpenStates bill ID

        Returns:
            DocumentSource or None if not found
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = {"X-API-Key": self.api_key}

            try:
                response = await client.get(
                    f"{self.BASE_URL}/bills/{bill_id}",
                    headers=headers,
                )
                response.raise_for_status()
                bill = response.json()
            except Exception as e:
                logger.error(
                    "Failed to fetch bill from OpenStates",
                    bill_id=bill_id,
                    error=str(e),
                )
                return None

            content = self._extract_bill_content(bill)
            if not content:
                return None

            # Get jurisdiction from bill and derive source name
            bill_jurisdiction = bill.get("jurisdiction", {}).get("id", "")
            source_name = self._get_source_name(bill_jurisdiction)

            metadata = self.metadata_extractor.extract_bill_metadata(
                bill,
                source=source_name,
            )

            return DocumentSource(
                content=content,
                metadata=metadata,
            )

    async def fetch_legislators(
        self,
        jurisdiction: str,
        limit: int = 100,
    ) -> AsyncIterator[DocumentSource]:
        """
        Fetch legislators for a jurisdiction.

        Args:
            jurisdiction: State abbreviation
            limit: Maximum number of legislators to fetch

        Yields:
            DocumentSource objects for each legislator
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = {
                "accept": "application/json",
                "X-API-Key": self.api_key,
            }

            # Include all available data for legislators
            include_params = [
                "other_names",
                "other_identifiers",
                "links",
                "sources",
                "offices",
            ]
            params = [
                ("jurisdiction", jurisdiction),
                ("per_page", min(limit, 50)),
            ] + [("include", p) for p in include_params]

            logger.info(
                "Fetching legislators from OpenStates",
                jurisdiction=jurisdiction,
            )

            try:
                response = await client.get(
                    f"{self.BASE_URL}/people",
                    params=params,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
            except Exception as e:
                logger.error("Failed to fetch legislators", error=str(e))
                return

            people = data.get("results", [])
            logger.info(f"Found {len(people)} legislators")

            for person in people:
                try:
                    content = self._extract_legislator_content(person)
                    if not content:
                        continue

                    # Use jurisdiction-based source name
                    source_name = self._get_source_name(jurisdiction)

                    metadata = self.metadata_extractor.extract_legislator_metadata(
                        {
                            "id": person.get("id"),
                            "name": person.get("name"),
                            "party": person.get("party"),
                            "state": jurisdiction.upper(),
                            "chamber": person.get("current_role", {}).get("org_classification"),
                            "district": person.get("current_role", {}).get("district"),
                        },
                        source=source_name,
                    )

                    yield DocumentSource(
                        content=content,
                        metadata=metadata,
                    )

                except Exception as e:
                    logger.warning(
                        "Failed to process legislator",
                        person=person.get("name"),
                        error=str(e),
                    )
                    continue

    async def fetch_legislator_by_id(
        self,
        person_id: str,
        max_retries: int = 3,
    ) -> DocumentSource | None:
        """
        Fetch a single legislator by OpenStates ID.

        Args:
            person_id: OpenStates person ID (e.g., "ocd-person/...")
            max_retries: Maximum retries for rate limit errors

        Returns:
            DocumentSource or None if not found
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = {
                "accept": "application/json",
                "X-API-Key": self.api_key,
            }
            # OpenStates v3 API uses query param, not path param
            url = f"{self.BASE_URL}/people"

            # Include all available data for legislators
            include_params = [
                "other_names",
                "other_identifiers",
                "links",
                "sources",
                "offices",
            ]
            params = [("id", person_id)] + [("include", p) for p in include_params]

            logger.debug(
                "Fetching legislator from OpenStates",
                url=url,
                person_id=person_id,
            )

            for attempt in range(max_retries):
                try:
                    response = await client.get(
                        url,
                        headers=headers,
                        params=params,
                    )

                    # Handle rate limiting with retry
                    if response.status_code == 429:
                        retry_after = int(response.headers.get("Retry-After", 2))
                        logger.warning(
                            f"Rate limited by OpenStates, waiting {retry_after}s "
                            f"(attempt {attempt + 1}/{max_retries})",
                            person_id=person_id,
                        )
                        await asyncio.sleep(retry_after)
                        continue

                    response.raise_for_status()
                    data = response.json()
                    results = data.get("results", [])

                    if not results:
                        logger.debug(
                            "Legislator not found in OpenStates API",
                            person_id=person_id,
                        )
                        return None

                    person = results[0]
                    break  # Success, exit retry loop

                except httpx.HTTPStatusError as e:
                    logger.error(
                        "Failed to fetch legislator from OpenStates",
                        person_id=person_id,
                        status_code=e.response.status_code,
                        error=str(e),
                    )
                    return None
                except Exception as e:
                    logger.error(
                        "Failed to fetch legislator from OpenStates",
                        person_id=person_id,
                        error=str(e),
                    )
                    return None
            else:
                # Exhausted retries
                logger.error(
                    "Exhausted retries fetching legislator from OpenStates",
                    person_id=person_id,
                )
                return None

            content = self._extract_legislator_content(person)
            if not content:
                return None

            # Extract jurisdiction from current role
            current_role = person.get("current_role", {})
            # Map jurisdiction to state abbreviation
            state = ""
            if person.get("jurisdiction", {}).get("classification") == "state":
                # Extract state from jurisdiction ID
                # Format: "ocd-jurisdiction/country:us/state:fl/government"
                jur_id = person.get("jurisdiction", {}).get("id", "")
                if "state:" in jur_id:
                    # Extract the state code after "state:"
                    state_part = jur_id.split("state:")[1]
                    state = state_part.split("/")[0].upper()
                else:
                    # Fallback: look for 2-letter part
                    parts = jur_id.split("/")
                    for part in parts:
                        if len(part) == 2 and part.isalpha():
                            state = part.upper()
                            break

            # Use jurisdiction-based source name
            source_name = self._get_source_name(state)

            metadata = self.metadata_extractor.extract_legislator_metadata(
                {
                    "id": person.get("id"),
                    "name": person.get("name"),
                    "party": person.get("party"),
                    "state": state,
                    "chamber": current_role.get("org_classification"),
                    "district": current_role.get("district"),
                    "email": person.get("email"),
                    "image": person.get("image"),
                    "links": person.get("links", []),
                    "offices": person.get("offices", []),
                    "current_role": current_role,
                },
                source=source_name,
            )

            return DocumentSource(
                content=content,
                metadata=metadata,
            )

    async def fetch_legislators_batch(
        self,
        person_ids: list[str],
        rate_limit: float = 0.5,  # 500ms = 2 calls/sec (OpenStates API tier limit)
    ) -> AsyncIterator[DocumentSource]:
        """
        Batch fetch legislators with rate limiting.

        Args:
            person_ids: List of OpenStates person IDs to fetch
            rate_limit: Seconds to wait between requests (default 0.5s for 2 calls/sec)

        Yields:
            DocumentSource objects for each successfully fetched legislator
        """
        logger.info(
            f"Batch fetching {len(person_ids)} legislators "
            f"(rate limit: {rate_limit}s)"
        )

        for i, person_id in enumerate(person_ids):
            try:
                doc = await self.fetch_legislator_by_id(person_id)
                if doc:
                    yield doc
                    logger.debug(
                        f"Fetched legislator {i + 1}/{len(person_ids)}: {person_id}"
                    )
                else:
                    logger.debug(f"No data for legislator: {person_id}")
            except Exception as e:
                logger.warning(
                    f"Failed to fetch legislator {person_id}: {e}"
                )

            # Rate limiting
            if i < len(person_ids) - 1 and rate_limit > 0:
                await asyncio.sleep(rate_limit)

    def _extract_legislator_content_detailed(self, person: dict) -> str:
        """
        Extract detailed text content from legislator data.

        This method provides richer content extraction than the basic
        _extract_legislator_content, including committees and contact details.

        Args:
            person: OpenStates person data

        Returns:
            Formatted text content for embedding
        """
        parts = []

        name = person.get("name", "Unknown")
        parts.append(f"# {name}")

        # Basic info
        if person.get("party"):
            parts.append(f"**Party:** {person['party']}")

        # Current role
        current_role = person.get("current_role", {})
        if current_role:
            parts.append("## Current Position")
            if current_role.get("title"):
                parts.append(f"- Title: {current_role['title']}")
            if current_role.get("org_classification"):
                chamber = current_role["org_classification"]
                chamber_display = "Senate" if chamber == "upper" else "House"
                parts.append(f"- Chamber: {chamber_display}")
            if current_role.get("district"):
                parts.append(f"- District: {current_role['district']}")

        # Offices/Contact info
        offices = person.get("offices", [])
        email = person.get("email") or person.get("capitol_email")
        if offices or email:
            parts.append("## Contact Information")
            if email:
                parts.append(f"- Email: {email}")
            for office in offices:
                office_type = office.get("classification", "Office")
                parts.append(f"\n**{office_type.title()}:**")
                if office.get("address"):
                    parts.append(f"- Address: {office['address']}")
                if office.get("voice"):
                    parts.append(f"- Phone: {office['voice']}")
                if office.get("fax"):
                    parts.append(f"- Fax: {office['fax']}")

        # Links
        links = person.get("links", [])
        if links:
            parts.append("## External Links")
            for link in links:
                note = link.get("note", "Website")
                url = link.get("url", "")
                if url:
                    parts.append(f"- [{note}]({url})")

        return "\n\n".join(parts) if parts else ""

    def _extract_bill_content(self, bill: dict) -> str:
        """Extract text content from bill data."""
        parts = []

        # Title
        if bill.get("title"):
            parts.append(f"# {bill['title']}")

        # Identifier and session
        if bill.get("identifier"):
            parts.append(f"**Bill:** {bill['identifier']}")
        if bill.get("legislative_session", {}).get("name"):
            parts.append(f"**Session:** {bill['legislative_session']['name']}")

        # Classification
        if bill.get("classification"):
            parts.append(f"**Type:** {', '.join(bill['classification'])}")

        # Abstract/Summary
        abstracts = bill.get("abstracts", [])
        if abstracts:
            parts.append("## Summary")
            parts.append(abstracts[0].get("abstract", ""))

        # Sponsors (with DDP links when available)
        sponsorships = bill.get("sponsorships", [])
        if sponsorships:
            parts.append("## Sponsors")
            primary = [s for s in sponsorships if s.get("primary")]
            cosponsors = [s for s in sponsorships if not s.get("primary")]

            if primary:
                primary_formatted = self._format_sponsor_with_link(primary[0])
                parts.append(f"**Primary Sponsor:** {primary_formatted}")
            if cosponsors:
                cosponsor_formatted = [self._format_sponsor_with_link(s) for s in cosponsors[:30]]
                parts.append(f"**Cosponsors:** {', '.join(cosponsor_formatted)}")
                if len(cosponsors) > 30:
                    parts.append(f"  (and {len(cosponsors) - 30} more)")

        # Actions/History
        actions = bill.get("actions", [])
        if actions:
            parts.append("## Recent Actions")
            for action in actions[-5:]:  # Last 5 actions
                date = action.get("date", "")
                desc = action.get("description", "")
                parts.append(f"- {date}: {desc}")

        return "\n\n".join(parts) if parts else ""

    def _extract_legislator_content(self, person: dict) -> str:
        """Extract text content from legislator data."""
        parts = []

        name = person.get("name", "Unknown")
        parts.append(f"# {name}")

        # Basic info
        if person.get("party"):
            parts.append(f"**Party:** {person['party']}")

        # Current role
        current_role = person.get("current_role", {})
        if current_role:
            parts.append("## Current Position")
            parts.append(f"- Title: {current_role.get('title', 'Unknown')}")
            parts.append(f"- Chamber: {current_role.get('org_classification', 'Unknown')}")
            parts.append(f"- District: {current_role.get('district', 'Unknown')}")

        # Contact info
        contact = person.get("email") or person.get("capitol_email")
        if contact:
            parts.append("## Contact")
            parts.append(f"- Email: {contact}")

        # Links
        links = person.get("links", [])
        if links:
            parts.append("## Links")
            for link in links:
                parts.append(f"- [{link.get('note', 'Link')}]({link.get('url', '')})")

        return "\n\n".join(parts) if parts else ""
