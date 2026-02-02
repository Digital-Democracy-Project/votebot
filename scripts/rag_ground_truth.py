#!/usr/bin/env python3
"""
Ground Truth Extraction for RAG Testing.

Fetches structured data from Webflow CMS and OpenStates API to use as
ground truth for validating RAG responses.
"""

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from bs4 import BeautifulSoup


@dataclass
class BillGroundTruth:
    """Ground truth data for a bill."""

    webflow_id: str
    slug: str
    name: str
    bill_prefix: str
    bill_number: str
    bill_id: str  # e.g., "HB 363"
    session: str
    jurisdiction: str  # State code (e.g., "FL")
    jurisdiction_name: str  # Full name (e.g., "Florida")
    description: str
    support_text: str
    oppose_text: str
    status: str
    gov_url: str
    # Resolved organization names
    support_org_names: list[str] = field(default_factory=list)
    oppose_org_names: list[str] = field(default_factory=list)
    # OpenStates enrichment
    sponsors: list[str] = field(default_factory=list)
    latest_action: str = ""
    action_date: str = ""

    def description_keywords(self, min_length: int = 4) -> list[str]:
        """Extract significant keywords from description."""
        return self._extract_keywords(self.description, min_length)

    def support_keywords(self, min_length: int = 4) -> list[str]:
        """Extract keywords from support arguments."""
        return self._extract_keywords(self.support_text, min_length)

    def oppose_keywords(self, min_length: int = 4) -> list[str]:
        """Extract keywords from oppose arguments."""
        return self._extract_keywords(self.oppose_text, min_length)

    def _extract_keywords(self, text: str, min_length: int) -> list[str]:
        """Extract significant words from text."""
        if not text:
            return []
        # Remove common stop words
        stop_words = {
            "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
            "of", "with", "by", "from", "as", "is", "was", "are", "were", "been",
            "be", "have", "has", "had", "do", "does", "did", "will", "would",
            "could", "should", "may", "might", "must", "shall", "can", "this",
            "that", "these", "those", "it", "its", "they", "their", "them",
            "which", "who", "whom", "whose", "what", "when", "where", "why",
            "how", "all", "each", "every", "both", "few", "more", "most",
            "other", "some", "such", "no", "nor", "not", "only", "own", "same",
            "so", "than", "too", "very", "just", "also", "now", "any", "into",
        }
        # Extract words, removing punctuation
        words = re.findall(r'\b[a-zA-Z]+\b', text.lower())
        # Filter by length and stop words, deduplicate
        keywords = []
        seen = set()
        for word in words:
            if len(word) >= min_length and word not in stop_words and word not in seen:
                keywords.append(word)
                seen.add(word)
        return keywords[:20]  # Limit to top 20

    @property
    def has_support_orgs(self) -> bool:
        return len(self.support_org_names) > 0

    @property
    def has_oppose_orgs(self) -> bool:
        return len(self.oppose_org_names) > 0

    @property
    def has_support_text(self) -> bool:
        return bool(self.support_text and len(self.support_text) > 20)

    @property
    def has_oppose_text(self) -> bool:
        return bool(self.oppose_text and len(self.oppose_text) > 20)


@dataclass
class LegislatorGroundTruth:
    """Ground truth data for a legislator."""

    webflow_id: str
    slug: str
    name: str
    openstates_id: str
    party: str
    chamber: str
    district: str
    jurisdiction: str  # State code
    jurisdiction_name: str  # Full name
    score: str
    # OpenStates enrichment
    sponsored_bills: list[str] = field(default_factory=list)

    @property
    def has_scorecard(self) -> bool:
        return self.score is not None and self.score != ""

    @property
    def chamber_title(self) -> str:
        """Return chamber title (Representative/Senator)."""
        chamber_lower = self.chamber.lower() if self.chamber else ""
        if "senate" in chamber_lower or "upper" in chamber_lower:
            return "Senator"
        return "Representative"


@dataclass
class OrganizationGroundTruth:
    """Ground truth data for an organization."""

    webflow_id: str
    slug: str
    name: str
    org_type: str
    about: str
    website: str
    # Resolved bill names
    bills_support_names: list[str] = field(default_factory=list)
    bills_oppose_names: list[str] = field(default_factory=list)

    def about_keywords(self, min_length: int = 4) -> list[str]:
        """Extract significant keywords from about text."""
        if not self.about:
            return []
        stop_words = {
            "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
            "of", "with", "by", "from", "as", "is", "was", "are", "were", "been",
            "be", "have", "has", "had", "this", "that", "which", "who", "their",
        }
        words = re.findall(r'\b[a-zA-Z]+\b', self.about.lower())
        keywords = []
        seen = set()
        for word in words:
            if len(word) >= min_length and word not in stop_words and word not in seen:
                keywords.append(word)
                seen.add(word)
        return keywords[:15]

    @property
    def has_type(self) -> bool:
        return bool(self.org_type)

    @property
    def has_supported_bills(self) -> bool:
        return len(self.bills_support_names) > 0

    @property
    def has_opposed_bills(self) -> bool:
        return len(self.bills_oppose_names) > 0

    @property
    def has_about(self) -> bool:
        return bool(self.about and len(self.about) > 20)


# State code to full name mapping
STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
    "US": "Federal", "PR": "Puerto Rico",
}


class GroundTruthFetcher:
    """Fetches ground truth data from Webflow CMS and OpenStates."""

    WEBFLOW_BASE_URL = "https://api.webflow.com/v2"
    OPENSTATES_BASE_URL = "https://v3.openstates.org"

    def __init__(
        self,
        webflow_api_key: str,
        bills_collection_id: str,
        legislators_collection_id: str,
        organizations_collection_id: str,
        jurisdiction_collection_id: str,
        openstates_api_key: str | None = None,
    ):
        self.webflow_api_key = webflow_api_key
        self.bills_collection_id = bills_collection_id
        self.legislators_collection_id = legislators_collection_id
        self.organizations_collection_id = organizations_collection_id
        self.jurisdiction_collection_id = jurisdiction_collection_id
        self.openstates_api_key = openstates_api_key

        # Caches
        self._jurisdiction_cache: dict[str, str] = {}
        self._bill_cache: dict[str, dict] = {}
        self._organization_cache: dict[str, dict] = {}

    async def fetch_all_bills(
        self,
        limit: int = 0,
        jurisdiction: str | None = None,
    ) -> list[BillGroundTruth]:
        """Fetch all bills from Webflow CMS."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            headers = {
                "Authorization": f"Bearer {self.webflow_api_key}",
                "accept": "application/json",
            }

            # Build caches
            await self._build_jurisdiction_cache(client, headers)
            await self._build_organization_cache(client, headers)

            bills = []
            offset = 0
            page_size = 100

            while True:
                params = {"limit": page_size, "offset": offset}
                response = await client.get(
                    f"{self.WEBFLOW_BASE_URL}/collections/{self.bills_collection_id}/items",
                    headers=headers,
                    params=params,
                )
                response.raise_for_status()
                data = response.json()
                items = data.get("items", [])

                if not items:
                    break

                for item in items:
                    bill = await self._process_bill_item(item, client, headers)
                    if bill:
                        # Filter by jurisdiction if specified
                        if jurisdiction and bill.jurisdiction != jurisdiction.upper():
                            continue
                        bills.append(bill)
                        if limit > 0 and len(bills) >= limit:
                            return bills

                pagination = data.get("pagination", {})
                total = pagination.get("total", 0)
                if offset + len(items) >= total or len(items) < page_size:
                    break

                offset += page_size

            return bills

    async def fetch_all_legislators(
        self,
        limit: int = 0,
        jurisdiction: str | None = None,
    ) -> list[LegislatorGroundTruth]:
        """Fetch all legislators from Webflow CMS."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            headers = {
                "Authorization": f"Bearer {self.webflow_api_key}",
                "accept": "application/json",
            }

            await self._build_jurisdiction_cache(client, headers)

            legislators = []
            offset = 0
            page_size = 100

            while True:
                params = {"limit": page_size, "offset": offset}
                response = await client.get(
                    f"{self.WEBFLOW_BASE_URL}/collections/{self.legislators_collection_id}/items",
                    headers=headers,
                    params=params,
                )
                response.raise_for_status()
                data = response.json()
                items = data.get("items", [])

                if not items:
                    break

                for item in items:
                    legislator = self._process_legislator_item(item)
                    if legislator:
                        if jurisdiction and legislator.jurisdiction != jurisdiction.upper():
                            continue
                        legislators.append(legislator)
                        if limit > 0 and len(legislators) >= limit:
                            return legislators

                pagination = data.get("pagination", {})
                total = pagination.get("total", 0)
                if offset + len(items) >= total or len(items) < page_size:
                    break

                offset += page_size

            return legislators

    async def fetch_all_organizations(
        self,
        limit: int = 0,
    ) -> list[OrganizationGroundTruth]:
        """Fetch all organizations from Webflow CMS."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            headers = {
                "Authorization": f"Bearer {self.webflow_api_key}",
                "accept": "application/json",
            }

            await self._build_bill_cache(client, headers)

            organizations = []
            offset = 0
            page_size = 100

            while True:
                params = {"limit": page_size, "offset": offset}
                response = await client.get(
                    f"{self.WEBFLOW_BASE_URL}/collections/{self.organizations_collection_id}/items",
                    headers=headers,
                    params=params,
                )
                response.raise_for_status()
                data = response.json()
                items = data.get("items", [])

                if not items:
                    break

                for item in items:
                    org = self._process_organization_item(item)
                    if org:
                        organizations.append(org)
                        if limit > 0 and len(organizations) >= limit:
                            return organizations

                pagination = data.get("pagination", {})
                total = pagination.get("total", 0)
                if offset + len(items) >= total or len(items) < page_size:
                    break

                offset += page_size

            return organizations

    async def enrich_with_openstates(
        self,
        bills: list[BillGroundTruth],
        legislators: list[LegislatorGroundTruth],
    ) -> None:
        """Enrich ground truth with OpenStates data."""
        if not self.openstates_api_key:
            return

        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = {"X-API-KEY": self.openstates_api_key}

            # Enrich bills with sponsor info
            for bill in bills:
                if bill.jurisdiction == "US":
                    continue  # OpenStates doesn't have federal bills
                try:
                    await self._enrich_bill_from_openstates(bill, client, headers)
                except Exception:
                    pass  # Silently skip on error

            # Enrich legislators with sponsored bills
            for legislator in legislators:
                if legislator.jurisdiction == "US":
                    continue
                try:
                    await self._enrich_legislator_from_openstates(legislator, client, headers)
                except Exception:
                    pass

    async def _enrich_bill_from_openstates(
        self,
        bill: BillGroundTruth,
        client: httpx.AsyncClient,
        headers: dict,
    ) -> None:
        """Fetch sponsor and status from OpenStates."""
        # Search for the bill
        params = {
            "jurisdiction": bill.jurisdiction.lower(),
            "identifier": f"{bill.bill_prefix} {bill.bill_number}",
        }
        response = await client.get(
            f"{self.OPENSTATES_BASE_URL}/bills",
            headers=headers,
            params=params,
        )
        if response.status_code != 200:
            return

        data = response.json()
        results = data.get("results", [])
        if not results:
            return

        # Get the first (most recent) match
        bill_data = results[0]

        # Extract sponsors
        sponsorships = bill_data.get("sponsorships", [])
        bill.sponsors = [
            s.get("name", "") for s in sponsorships
            if s.get("name")
        ]

        # Extract latest action
        actions = bill_data.get("actions", [])
        if actions:
            latest = actions[-1]
            bill.latest_action = latest.get("description", "")
            bill.action_date = latest.get("date", "")

    async def _enrich_legislator_from_openstates(
        self,
        legislator: LegislatorGroundTruth,
        client: httpx.AsyncClient,
        headers: dict,
    ) -> None:
        """Fetch sponsored bills from OpenStates."""
        if not legislator.openstates_id:
            return

        # Get bills sponsored by this legislator
        params = {
            "sponsor": legislator.openstates_id,
            "per_page": 10,
        }
        response = await client.get(
            f"{self.OPENSTATES_BASE_URL}/bills",
            headers=headers,
            params=params,
        )
        if response.status_code != 200:
            return

        data = response.json()
        results = data.get("results", [])

        legislator.sponsored_bills = [
            f"{b.get('identifier', '')} - {b.get('title', '')[:50]}"
            for b in results
            if b.get("identifier")
        ]

    async def _process_bill_item(
        self,
        item: dict,
        client: httpx.AsyncClient,
        headers: dict,
    ) -> BillGroundTruth | None:
        """Process a Webflow bill item into ground truth."""
        fields = item.get("fieldData", {})
        name = fields.get("name", "")
        if not name:
            return None

        webflow_id = item.get("id", "")
        slug = fields.get("slug", "")
        bill_prefix = fields.get("bill-prefix", "")
        bill_number = fields.get("bill-number", "")
        bill_id = f"{bill_prefix} {bill_number}".strip()

        # Resolve jurisdiction
        jurisdiction_ref = fields.get("jurisdiction")
        jurisdiction = self._resolve_jurisdiction(jurisdiction_ref)
        jurisdiction_name = STATE_NAMES.get(jurisdiction, jurisdiction)

        # Resolve organization references
        support_org_ids = fields.get("member-organizations", [])
        oppose_org_ids = fields.get("organizations-oppose", [])

        support_org_names = [
            self._organization_cache.get(org_id, {}).get("name", "")
            for org_id in support_org_ids
            if org_id in self._organization_cache
        ]
        oppose_org_names = [
            self._organization_cache.get(org_id, {}).get("name", "")
            for org_id in oppose_org_ids
            if org_id in self._organization_cache
        ]

        # Extract text content
        description = self._html_to_text(fields.get("description", ""))
        support_text = self._html_to_text(fields.get("support", ""))
        oppose_text = self._html_to_text(fields.get("oppose", ""))

        return BillGroundTruth(
            webflow_id=webflow_id,
            slug=slug,
            name=name,
            bill_prefix=bill_prefix,
            bill_number=bill_number,
            bill_id=bill_id,
            session=fields.get("session-code", fields.get("bill-session", "")),
            jurisdiction=jurisdiction,
            jurisdiction_name=jurisdiction_name,
            description=description,
            support_text=support_text,
            oppose_text=oppose_text,
            status=fields.get("status", ""),
            gov_url=fields.get("gov-url", ""),
            support_org_names=support_org_names,
            oppose_org_names=oppose_org_names,
        )

    def _process_legislator_item(self, item: dict) -> LegislatorGroundTruth | None:
        """Process a Webflow legislator item into ground truth."""
        fields = item.get("fieldData", {})
        name = fields.get("name", "")
        openstates_id = fields.get("openstatesid", "")

        if not name or not openstates_id:
            return None

        jurisdiction_ref = fields.get("jurisdiction")
        jurisdiction = self._resolve_jurisdiction(jurisdiction_ref)
        jurisdiction_name = STATE_NAMES.get(jurisdiction, jurisdiction)

        return LegislatorGroundTruth(
            webflow_id=item.get("id", ""),
            slug=fields.get("slug", ""),
            name=name,
            openstates_id=openstates_id,
            party=fields.get("party-2", fields.get("party", "")),
            chamber=fields.get("chamber", ""),
            district=str(fields.get("district", "")),
            jurisdiction=jurisdiction,
            jurisdiction_name=jurisdiction_name,
            score=str(fields.get("score", "")),
        )

    def _process_organization_item(self, item: dict) -> OrganizationGroundTruth | None:
        """Process a Webflow organization item into ground truth."""
        fields = item.get("fieldData", {})
        name = fields.get("name", "")

        if not name:
            return None

        # Resolve bill references
        bills_support_ids = fields.get("bills-support", [])
        bills_oppose_ids = fields.get("bills-oppose", [])

        bills_support_names = [
            self._bill_cache.get(bill_id, {}).get("identifier", "")
            for bill_id in bills_support_ids
            if bill_id in self._bill_cache
        ]
        bills_oppose_names = [
            self._bill_cache.get(bill_id, {}).get("identifier", "")
            for bill_id in bills_oppose_ids
            if bill_id in self._bill_cache
        ]

        about = self._html_to_text(fields.get("about-organization", ""))

        return OrganizationGroundTruth(
            webflow_id=item.get("id", ""),
            slug=fields.get("slug", ""),
            name=name,
            org_type=fields.get("type-2", ""),
            about=about,
            website=fields.get("website", ""),
            bills_support_names=bills_support_names,
            bills_oppose_names=bills_oppose_names,
        )

    def _resolve_jurisdiction(self, jurisdiction_ref: str | list | None) -> str:
        """Resolve a jurisdiction reference to a state code."""
        if not jurisdiction_ref:
            return "US"

        if isinstance(jurisdiction_ref, list):
            jurisdiction_ref = jurisdiction_ref[0] if jurisdiction_ref else ""

        if isinstance(jurisdiction_ref, str) and len(jurisdiction_ref) == 2:
            return jurisdiction_ref.upper()

        if isinstance(jurisdiction_ref, str):
            return self._jurisdiction_cache.get(jurisdiction_ref, "US")

        return "US"

    async def _build_jurisdiction_cache(
        self,
        client: httpx.AsyncClient,
        headers: dict,
    ) -> None:
        """Build jurisdiction ID to state code mapping."""
        if self._jurisdiction_cache:
            return

        try:
            offset = 0
            while True:
                response = await client.get(
                    f"{self.WEBFLOW_BASE_URL}/collections/{self.jurisdiction_collection_id}/items",
                    headers=headers,
                    params={"limit": 100, "offset": offset},
                )
                response.raise_for_status()
                data = response.json()
                items = data.get("items", [])

                if not items:
                    break

                for item in items:
                    item_id = item.get("id", "")
                    fields = item.get("fieldData", {})
                    state_code = (
                        fields.get("state-code")
                        or fields.get("code")
                        or fields.get("abbreviation")
                        or fields.get("name", "")[:2].upper()
                    )
                    if item_id and state_code:
                        self._jurisdiction_cache[item_id] = state_code

                pagination = data.get("pagination", {})
                if offset + len(items) >= pagination.get("total", 0):
                    break
                offset += 100
        except Exception:
            pass

    async def _build_organization_cache(
        self,
        client: httpx.AsyncClient,
        headers: dict,
    ) -> None:
        """Build organization ID to name mapping."""
        if self._organization_cache:
            return

        try:
            offset = 0
            while True:
                response = await client.get(
                    f"{self.WEBFLOW_BASE_URL}/collections/{self.organizations_collection_id}/items",
                    headers=headers,
                    params={"limit": 100, "offset": offset},
                )
                response.raise_for_status()
                data = response.json()
                items = data.get("items", [])

                if not items:
                    break

                for item in items:
                    item_id = item.get("id", "")
                    fields = item.get("fieldData", {})
                    name = fields.get("name", "")
                    if item_id and name:
                        self._organization_cache[item_id] = {
                            "name": name,
                            "type": fields.get("type-2", ""),
                        }

                pagination = data.get("pagination", {})
                if offset + len(items) >= pagination.get("total", 0):
                    break
                offset += 100
        except Exception:
            pass

    async def _build_bill_cache(
        self,
        client: httpx.AsyncClient,
        headers: dict,
    ) -> None:
        """Build bill ID to identifier mapping."""
        if self._bill_cache:
            return

        try:
            offset = 0
            while True:
                response = await client.get(
                    f"{self.WEBFLOW_BASE_URL}/collections/{self.bills_collection_id}/items",
                    headers=headers,
                    params={"limit": 100, "offset": offset},
                )
                response.raise_for_status()
                data = response.json()
                items = data.get("items", [])

                if not items:
                    break

                for item in items:
                    item_id = item.get("id", "")
                    fields = item.get("fieldData", {})
                    name = fields.get("name", "")
                    identifier = f"{fields.get('bill-prefix', '')} {fields.get('bill-number', '')}".strip()
                    if item_id:
                        self._bill_cache[item_id] = {
                            "name": name,
                            "identifier": identifier or name,
                        }

                pagination = data.get("pagination", {})
                if offset + len(items) >= pagination.get("total", 0):
                    break
                offset += 100
        except Exception:
            pass

    def _html_to_text(self, html: str) -> str:
        """Convert HTML to plain text."""
        if not html:
            return ""
        if "<" not in html:
            return html
        soup = BeautifulSoup(html, "html.parser")
        return soup.get_text(separator=" ", strip=True)


async def main():
    """Test the ground truth fetcher."""
    import os
    from dotenv import load_dotenv

    load_dotenv()

    fetcher = GroundTruthFetcher(
        webflow_api_key=os.environ["WEBFLOW_API_KEY"],
        bills_collection_id=os.environ["WEBFLOW_BILLS_COLLECTION_ID"],
        legislators_collection_id=os.environ["WEBFLOW_LEGISLATORS_COLLECTION_ID"],
        organizations_collection_id=os.environ["WEBFLOW_ORGANIZATIONS_COLLECTION_ID"],
        jurisdiction_collection_id=os.environ["WEBFLOW_JURISDICTION_COLLECTION_ID"],
        openstates_api_key=os.environ.get("OPENSTATES_API_KEY"),
    )

    print("Fetching bills...")
    bills = await fetcher.fetch_all_bills(limit=5)
    for bill in bills:
        print(f"  - {bill.jurisdiction} {bill.bill_id}: {bill.name[:50]}...")
        print(f"    Keywords: {bill.description_keywords()[:5]}")

    print("\nFetching legislators...")
    legislators = await fetcher.fetch_all_legislators(limit=5)
    for leg in legislators:
        print(f"  - {leg.name} ({leg.party}) - {leg.jurisdiction} District {leg.district}")

    print("\nFetching organizations...")
    orgs = await fetcher.fetch_all_organizations(limit=5)
    for org in orgs:
        print(f"  - {org.name} ({org.org_type})")
        print(f"    Supports: {len(org.bills_support_names)} bills")


if __name__ == "__main__":
    asyncio.run(main())
