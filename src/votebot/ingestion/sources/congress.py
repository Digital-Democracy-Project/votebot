"""Congress.gov API data source connector."""

from typing import AsyncIterator

import httpx
import structlog

from votebot.config import Settings, get_settings
from votebot.ingestion.metadata import MetadataExtractor
from votebot.ingestion.pipeline import DocumentSource

logger = structlog.get_logger()


class CongressAPISource:
    """
    Data source connector for Congress.gov API.

    Fetches:
    - Bill text and summaries
    - Amendments
    - Votes
    - Legislator information
    """

    BASE_URL = "https://api.congress.gov/v3"

    def __init__(
        self,
        settings: Settings | None = None,
        metadata_extractor: MetadataExtractor | None = None,
    ):
        """
        Initialize the Congress.gov source.

        Args:
            settings: Application settings
            metadata_extractor: Metadata extractor instance
        """
        self.settings = settings or get_settings()
        self.metadata_extractor = metadata_extractor or MetadataExtractor()
        self.api_key = self.settings.congress_api_key.get_secret_value()

    async def fetch(
        self,
        congress: int | None = None,
        bill_type: str | None = None,
        limit: int = 100,
        **kwargs,
    ) -> AsyncIterator[DocumentSource]:
        """
        Fetch bills from Congress.gov.

        Args:
            congress: Congress number (e.g., 118)
            bill_type: Type of bill (hr, s, hjres, sjres)
            limit: Maximum number of bills to fetch

        Yields:
            DocumentSource objects for each bill
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Build URL
            url = f"{self.BASE_URL}/bill"
            if congress:
                url = f"{url}/{congress}"
            if bill_type:
                url = f"{url}/{bill_type}"

            params = {
                "api_key": self.api_key,
                "limit": min(limit, 250),  # API max
                "format": "json",
            }

            logger.info(
                "Fetching bills from Congress.gov",
                congress=congress,
                bill_type=bill_type,
                limit=limit,
            )

            try:
                response = await client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
            except Exception as e:
                logger.error("Failed to fetch from Congress.gov", error=str(e))
                return

            bills = data.get("bills", [])
            logger.info(f"Found {len(bills)} bills")

            for bill in bills:
                try:
                    # Fetch full bill details
                    bill_url = bill.get("url")
                    if bill_url:
                        detail_response = await client.get(
                            bill_url,
                            params={"api_key": self.api_key, "format": "json"},
                        )
                        detail_response.raise_for_status()
                        bill_detail = detail_response.json().get("bill", bill)
                    else:
                        bill_detail = bill

                    # Extract content
                    content = self._extract_bill_content(bill_detail)
                    if not content:
                        continue

                    # Extract metadata
                    metadata = self.metadata_extractor.extract_bill_metadata(
                        bill_detail,
                        source="congress.gov",
                    )

                    yield DocumentSource(
                        content=content,
                        metadata=metadata,
                    )

                except Exception as e:
                    logger.warning(
                        "Failed to process bill",
                        bill=bill.get("number"),
                        error=str(e),
                    )
                    continue

    async def fetch_bill(
        self,
        congress: int,
        bill_type: str,
        bill_number: int,
    ) -> DocumentSource | None:
        """
        Fetch a specific bill.

        Args:
            congress: Congress number
            bill_type: Type of bill
            bill_number: Bill number

        Returns:
            DocumentSource or None if not found
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            url = f"{self.BASE_URL}/bill/{congress}/{bill_type}/{bill_number}"
            params = {
                "api_key": self.api_key,
                "format": "json",
            }

            try:
                response = await client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
            except Exception as e:
                logger.error(
                    "Failed to fetch bill",
                    congress=congress,
                    bill_type=bill_type,
                    bill_number=bill_number,
                    error=str(e),
                )
                return None

            bill = data.get("bill", {})
            content = self._extract_bill_content(bill)

            if not content:
                return None

            metadata = self.metadata_extractor.extract_bill_metadata(
                bill,
                source="congress.gov",
            )

            return DocumentSource(
                content=content,
                metadata=metadata,
            )

    async def fetch_legislator(self, bioguide_id: str) -> DocumentSource | None:
        """
        Fetch a specific legislator.

        Args:
            bioguide_id: Bioguide ID of the legislator

        Returns:
            DocumentSource or None if not found
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            url = f"{self.BASE_URL}/member/{bioguide_id}"
            params = {
                "api_key": self.api_key,
                "format": "json",
            }

            try:
                response = await client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
            except Exception as e:
                logger.error(
                    "Failed to fetch legislator",
                    bioguide_id=bioguide_id,
                    error=str(e),
                )
                return None

            member = data.get("member", {})
            content = self._extract_member_content(member)

            if not content:
                return None

            metadata = self.metadata_extractor.extract_legislator_metadata(
                {
                    "bioguide_id": bioguide_id,
                    "name": member.get("directOrderName"),
                    "party": member.get("partyName"),
                    "state": member.get("state"),
                    "chamber": member.get("terms", [{}])[-1].get("chamber"),
                },
                source="congress.gov",
            )

            return DocumentSource(
                content=content,
                metadata=metadata,
            )

    def _extract_bill_content(self, bill: dict) -> str:
        """Extract text content from bill data."""
        parts = []

        # Title
        if bill.get("title"):
            parts.append(f"# {bill['title']}")

        # Summary
        summaries = bill.get("summaries", {}).get("billSummaries", [])
        if summaries:
            latest_summary = summaries[-1]
            if latest_summary.get("text"):
                parts.append("## Summary")
                parts.append(latest_summary["text"])

        # Sponsors
        sponsors = []
        if bill.get("sponsors"):
            for sponsor in bill["sponsors"]:
                sponsors.append(sponsor.get("fullName", "Unknown"))
        if sponsors:
            parts.append("## Sponsors")
            parts.append(", ".join(sponsors))

        # Status
        if bill.get("latestAction"):
            parts.append("## Latest Action")
            action = bill["latestAction"]
            parts.append(f"{action.get('actionDate', '')}: {action.get('text', '')}")

        # Cosponsors count
        if bill.get("cosponsors"):
            parts.append(f"\n**Cosponsors:** {bill['cosponsors']}")

        return "\n\n".join(parts) if parts else ""

    def _extract_member_content(self, member: dict) -> str:
        """Extract text content from member data."""
        parts = []

        name = member.get("directOrderName", "Unknown")
        parts.append(f"# {name}")

        # Basic info
        if member.get("partyName"):
            parts.append(f"**Party:** {member['partyName']}")
        if member.get("state"):
            parts.append(f"**State:** {member['state']}")

        # Current term
        terms = member.get("terms", [])
        if terms:
            current = terms[-1]
            parts.append("## Current Term")
            parts.append(f"- Chamber: {current.get('chamber', 'Unknown')}")
            parts.append(f"- Start: {current.get('startYear', 'Unknown')}")

        # Biography
        if member.get("depiction", {}).get("attribution"):
            parts.append("## About")
            parts.append(member["depiction"]["attribution"])

        return "\n\n".join(parts) if parts else ""
