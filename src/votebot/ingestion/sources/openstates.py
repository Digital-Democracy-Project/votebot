"""OpenStates API data source connector."""

import asyncio
from typing import AsyncIterator

import httpx
import structlog

from votebot.config import Settings, get_settings
from votebot.ingestion.metadata import DocumentMetadata, MetadataExtractor
from votebot.ingestion.pipeline import DocumentSource

logger = structlog.get_logger()


class OpenStatesSource:
    """
    Data source connector for OpenStates API.

    Fetches state legislature data:
    - Bills
    - Votes
    - Legislators
    """

    BASE_URL = "https://v3.openstates.org"

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

                    # Extract metadata
                    metadata = self.metadata_extractor.extract_bill_metadata(
                        bill_detail,
                        source="openstates",
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

            metadata = self.metadata_extractor.extract_bill_metadata(
                bill,
                source="openstates",
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
            headers = {"X-API-Key": self.api_key}

            params = {
                "jurisdiction": jurisdiction,
                "per_page": min(limit, 50),
            }

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

                    metadata = self.metadata_extractor.extract_legislator_metadata(
                        {
                            "id": person.get("id"),
                            "name": person.get("name"),
                            "party": person.get("party"),
                            "state": jurisdiction.upper(),
                            "chamber": person.get("current_role", {}).get("org_classification"),
                            "district": person.get("current_role", {}).get("district"),
                        },
                        source="openstates",
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
            headers = {"X-API-Key": self.api_key}
            # OpenStates v3 API uses query param, not path param
            url = f"{self.BASE_URL}/people"

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
                        params={"id": person_id},
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
                source="openstates",
            )

            return DocumentSource(
                content=content,
                metadata=metadata,
            )

    async def fetch_legislators_batch(
        self,
        person_ids: list[str],
        rate_limit: float = 0.5,
    ) -> AsyncIterator[DocumentSource]:
        """
        Batch fetch legislators with rate limiting.

        Args:
            person_ids: List of OpenStates person IDs to fetch
            rate_limit: Seconds to wait between requests

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

        # Sponsors
        sponsorships = bill.get("sponsorships", [])
        if sponsorships:
            parts.append("## Sponsors")
            primary = [s for s in sponsorships if s.get("primary")]
            cosponsors = [s for s in sponsorships if not s.get("primary")]

            if primary:
                parts.append(f"**Primary Sponsor:** {primary[0].get('name', 'Unknown')}")
            if cosponsors:
                cosponsor_names = [s.get("name", "") for s in cosponsors[:10]]
                parts.append(f"**Cosponsors:** {', '.join(cosponsor_names)}")
                if len(cosponsors) > 10:
                    parts.append(f"  (and {len(cosponsors) - 10} more)")

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
