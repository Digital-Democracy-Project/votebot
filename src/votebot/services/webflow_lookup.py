"""Runtime Webflow CMS lookup for bill organization positions.

Fetches authoritative org position data directly from Webflow CMS at runtime,
bypassing Pinecone retrieval. This supplements RAG with accurate data when
similarity scores fall below threshold or org data is missing from the index.
"""

import asyncio
from dataclasses import dataclass, field

import httpx
import structlog

from votebot.config import Settings, get_settings

logger = structlog.get_logger()


@dataclass
class OrgPosition:
    """A single organization's stance on a bill."""

    name: str
    org_type: str
    slug: str
    position: str  # "support" or "oppose"


@dataclass
class BillOrgPositionsResult:
    """Result of looking up organization positions for a bill."""

    bill_name: str
    supporting_orgs: list[OrgPosition] = field(default_factory=list)
    opposing_orgs: list[OrgPosition] = field(default_factory=list)
    found: bool = False


@dataclass
class BillPosition:
    """A single bill's relationship with an organization."""

    name: str
    bill_id: str
    slug: str
    position: str  # "support" or "oppose"


@dataclass
class OrgBillPositionsResult:
    """Result of looking up bill positions for an organization."""

    org_name: str
    supported_bills: list[BillPosition] = field(default_factory=list)
    opposed_bills: list[BillPosition] = field(default_factory=list)
    found: bool = False


class WebflowLookupService:
    """
    Lightweight runtime Webflow CMS lookup service.

    Fetches bill organization positions directly from Webflow CMS
    for use as authoritative context in LLM responses.
    """

    BASE_URL = "https://api.webflow.com/v2"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.api_key = self.settings.webflow_api_key.get_secret_value()
        self.bills_collection_id = self.settings.webflow_bills_collection_id
        self.organizations_collection_id = self.settings.webflow_organizations_collection_id
        self._org_cache: dict[str, dict] = {}
        self._bill_cache: dict[str, dict] = {}

    async def get_bill_org_positions(
        self,
        webflow_id: str | None = None,
        slug: str | None = None,
    ) -> BillOrgPositionsResult:
        """
        Get organization positions for a bill from Webflow CMS.

        Args:
            webflow_id: Webflow item ID for direct lookup (preferred)
            slug: Bill slug for fallback search

        Returns:
            BillOrgPositionsResult with supporting and opposing orgs
        """
        if not webflow_id and not slug:
            logger.warning("No webflow_id or slug provided for org position lookup")
            return BillOrgPositionsResult(bill_name="", found=False)

        # Fetch the bill item
        bill_item = None
        if webflow_id:
            bill_item = await self._fetch_bill_by_id(webflow_id)
        if not bill_item and slug:
            bill_item = await self._fetch_bill_by_slug(slug)

        if not bill_item:
            logger.info(
                "Bill not found in Webflow CMS",
                webflow_id=webflow_id,
                slug=slug,
            )
            return BillOrgPositionsResult(bill_name="", found=False)

        fields = bill_item.get("fieldData", {})
        bill_name = fields.get("name", "")

        # Extract org reference ID lists
        support_refs = fields.get("member-organizations", [])
        oppose_refs = fields.get("organizations-oppose", [])

        if not support_refs and not oppose_refs:
            logger.info(
                "Bill has no organization positions in Webflow",
                bill_name=bill_name,
            )
            return BillOrgPositionsResult(bill_name=bill_name, found=True)

        # Resolve org references in parallel
        supporting_orgs = await self._resolve_org_references(support_refs, "support")
        opposing_orgs = await self._resolve_org_references(oppose_refs, "oppose")

        logger.info(
            "Fetched bill org positions from Webflow",
            bill_name=bill_name,
            supporting=len(supporting_orgs),
            opposing=len(opposing_orgs),
        )

        return BillOrgPositionsResult(
            bill_name=bill_name,
            supporting_orgs=supporting_orgs,
            opposing_orgs=opposing_orgs,
            found=True,
        )

    async def _fetch_bill_by_id(self, webflow_id: str) -> dict | None:
        """Fetch a bill item from Webflow by its ID."""
        url = f"{self.BASE_URL}/collections/{self.bills_collection_id}/items/{webflow_id}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "accept": "application/json",
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.get(url, headers=headers)
                if response.status_code == 404:
                    logger.warning("Bill not found in Webflow", webflow_id=webflow_id)
                    return None
                response.raise_for_status()
                return response.json()
            except Exception as e:
                logger.error(
                    "Failed to fetch bill from Webflow",
                    webflow_id=webflow_id,
                    error=str(e),
                )
                return None

    async def _fetch_bill_by_slug(self, slug: str) -> dict | None:
        """Fetch a bill item from Webflow by slug (paginated search fallback)."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "accept": "application/json",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            offset = 0
            page_size = 100

            while True:
                try:
                    params = {"limit": page_size, "offset": offset}
                    response = await client.get(
                        f"{self.BASE_URL}/collections/{self.bills_collection_id}/items",
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
                        if fields.get("slug") == slug:
                            return item

                    pagination = data.get("pagination", {})
                    total = pagination.get("total", 0)
                    if offset + len(items) >= total or len(items) < page_size:
                        break

                    offset += page_size

                except Exception as e:
                    logger.error(
                        "Failed to search Webflow bills by slug",
                        slug=slug,
                        error=str(e),
                    )
                    break

        logger.warning("Bill not found by slug in Webflow", slug=slug)
        return None

    async def _resolve_org_references(
        self,
        org_refs: list,
        position: str,
    ) -> list[OrgPosition]:
        """Resolve organization reference IDs to OrgPosition objects in parallel."""
        if not org_refs:
            return []

        tasks = []
        for ref in org_refs:
            if isinstance(ref, str):
                tasks.append(self._fetch_org_by_id(ref))
            elif isinstance(ref, dict):
                # Already resolved
                tasks.append(asyncio.coroutine(lambda r=ref: r)() if False else None)  # noqa: handled below

        # Separate already-resolved dicts from IDs needing fetch
        resolved = []
        fetch_tasks = []
        fetch_indices = []

        for i, ref in enumerate(org_refs):
            if isinstance(ref, dict):
                resolved.append((i, ref))
            elif isinstance(ref, str):
                fetch_tasks.append(self._fetch_org_by_id(ref))
                fetch_indices.append(i)

        # Fetch all in parallel
        if fetch_tasks:
            fetched = await asyncio.gather(*fetch_tasks, return_exceptions=True)
            for idx, result in zip(fetch_indices, fetched):
                if isinstance(result, Exception):
                    logger.warning("Failed to fetch org", error=str(result))
                elif result:
                    resolved.append((idx, result))

        # Sort by original order and convert to OrgPosition
        resolved.sort(key=lambda x: x[0])
        return [
            OrgPosition(
                name=org_info.get("name", "Unknown"),
                org_type=org_info.get("type", ""),
                slug=org_info.get("slug", ""),
                position=position,
            )
            for _, org_info in resolved
        ]

    async def _fetch_org_by_id(self, org_id: str) -> dict | None:
        """Fetch a single organization from Webflow with in-memory caching."""
        # Check cache first
        if org_id in self._org_cache:
            return self._org_cache[org_id]

        if not self.organizations_collection_id:
            return None

        url = f"{self.BASE_URL}/collections/{self.organizations_collection_id}/items/{org_id}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "accept": "application/json",
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.get(url, headers=headers)
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                item = response.json()

                fields = item.get("fieldData", {})
                org_info = {
                    "name": fields.get("name", ""),
                    "type": fields.get("type-2", ""),
                    "slug": fields.get("slug", ""),
                }

                if org_info["name"]:
                    self._org_cache[org_id] = org_info
                    return org_info

                return None

            except Exception as e:
                logger.warning(
                    "Failed to fetch organization from Webflow",
                    org_id=org_id,
                    error=str(e),
                )
                return None


    async def get_org_bill_positions(
        self,
        webflow_id: str | None = None,
        slug: str | None = None,
    ) -> OrgBillPositionsResult:
        """
        Get bill positions for an organization from Webflow CMS.

        Args:
            webflow_id: Webflow item ID for direct lookup (preferred)
            slug: Org slug for fallback search

        Returns:
            OrgBillPositionsResult with supported and opposed bills
        """
        if not webflow_id and not slug:
            logger.warning("No webflow_id or slug provided for org bill position lookup")
            return OrgBillPositionsResult(org_name="", found=False)

        # Fetch the org item
        org_item = None
        if webflow_id:
            org_item = await self._fetch_org_item_by_id(webflow_id)
        if not org_item and slug:
            org_item = await self._fetch_org_item_by_slug(slug)

        if not org_item:
            logger.info(
                "Organization not found in Webflow CMS",
                webflow_id=webflow_id,
                slug=slug,
            )
            return OrgBillPositionsResult(org_name="", found=False)

        fields = org_item.get("fieldData", {})
        org_name = fields.get("name", "")

        # Extract bill reference ID lists
        support_refs = fields.get("bills-support", [])
        oppose_refs = fields.get("bills-oppose", [])

        if not support_refs and not oppose_refs:
            logger.info(
                "Organization has no bill positions in Webflow",
                org_name=org_name,
            )
            return OrgBillPositionsResult(org_name=org_name, found=True)

        # Resolve bill references in parallel
        supported_bills = await self._resolve_bill_references(support_refs, "support")
        opposed_bills = await self._resolve_bill_references(oppose_refs, "oppose")

        logger.info(
            "Fetched org bill positions from Webflow",
            org_name=org_name,
            supported=len(supported_bills),
            opposed=len(opposed_bills),
        )

        return OrgBillPositionsResult(
            org_name=org_name,
            supported_bills=supported_bills,
            opposed_bills=opposed_bills,
            found=True,
        )

    async def _fetch_org_item_by_id(self, webflow_id: str) -> dict | None:
        """Fetch a full organization item from Webflow by its ID (includes reference fields)."""
        if not self.organizations_collection_id:
            return None

        url = f"{self.BASE_URL}/collections/{self.organizations_collection_id}/items/{webflow_id}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "accept": "application/json",
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.get(url, headers=headers)
                if response.status_code == 404:
                    logger.warning("Organization not found in Webflow", webflow_id=webflow_id)
                    return None
                response.raise_for_status()
                return response.json()
            except Exception as e:
                logger.error(
                    "Failed to fetch organization from Webflow",
                    webflow_id=webflow_id,
                    error=str(e),
                )
                return None

    async def _fetch_org_item_by_slug(self, slug: str) -> dict | None:
        """Fetch an organization item from Webflow by slug (paginated search fallback)."""
        if not self.organizations_collection_id:
            return None

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "accept": "application/json",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            offset = 0
            page_size = 100

            while True:
                try:
                    params = {"limit": page_size, "offset": offset}
                    response = await client.get(
                        f"{self.BASE_URL}/collections/{self.organizations_collection_id}/items",
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
                        if fields.get("slug") == slug:
                            return item

                    pagination = data.get("pagination", {})
                    total = pagination.get("total", 0)
                    if offset + len(items) >= total or len(items) < page_size:
                        break

                    offset += page_size

                except Exception as e:
                    logger.error(
                        "Failed to search Webflow organizations by slug",
                        slug=slug,
                        error=str(e),
                    )
                    break

        logger.warning("Organization not found by slug in Webflow", slug=slug)
        return None

    async def _resolve_bill_references(
        self,
        bill_refs: list,
        position: str,
    ) -> list[BillPosition]:
        """Resolve bill reference IDs to BillPosition objects in parallel."""
        if not bill_refs:
            return []

        resolved = []
        fetch_tasks = []
        fetch_indices = []

        for i, ref in enumerate(bill_refs):
            if isinstance(ref, dict):
                resolved.append((i, ref))
            elif isinstance(ref, str):
                fetch_tasks.append(self._fetch_bill_info_by_id(ref))
                fetch_indices.append(i)

        # Fetch all in parallel
        if fetch_tasks:
            fetched = await asyncio.gather(*fetch_tasks, return_exceptions=True)
            for idx, result in zip(fetch_indices, fetched):
                if isinstance(result, Exception):
                    logger.warning("Failed to fetch bill", error=str(result))
                elif result:
                    resolved.append((idx, result))

        # Sort by original order and convert to BillPosition
        resolved.sort(key=lambda x: x[0])
        return [
            BillPosition(
                name=bill_info.get("name", "Unknown"),
                bill_id=bill_info.get("identifier", ""),
                slug=bill_info.get("slug", ""),
                position=position,
            )
            for _, bill_info in resolved
        ]

    async def _fetch_bill_info_by_id(self, bill_id: str) -> dict | None:
        """Fetch a single bill's info from Webflow with in-memory caching."""
        # Check cache first
        if bill_id in self._bill_cache:
            return self._bill_cache[bill_id]

        url = f"{self.BASE_URL}/collections/{self.bills_collection_id}/items/{bill_id}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "accept": "application/json",
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.get(url, headers=headers)
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                item = response.json()

                fields = item.get("fieldData", {})
                bill_info = {
                    "name": fields.get("name", ""),
                    "identifier": fields.get("bill-id", ""),
                    "slug": fields.get("slug", ""),
                }

                if bill_info["name"]:
                    self._bill_cache[bill_id] = bill_info
                    return bill_info

                return None

            except Exception as e:
                logger.warning(
                    "Failed to fetch bill from Webflow",
                    bill_id=bill_id,
                    error=str(e),
                )
                return None


def format_org_positions_context(result: BillOrgPositionsResult) -> str:
    """
    Format org positions as markdown for LLM context injection.

    Replicates the format from webflow.py bill ingestion with DDP links.
    Labeled as authoritative so the LLM trusts it over RAG context.

    Args:
        result: BillOrgPositionsResult from get_bill_org_positions

    Returns:
        Formatted markdown string, or empty string if no data
    """
    if not result.found:
        return ""

    if not result.supporting_orgs and not result.opposing_orgs:
        return (
            f"## Organization Positions (Authoritative Source — Webflow CMS)\n\n"
            f"No organizations have recorded positions on this bill in the Digital Democracy Project database."
        )

    parts = ["## Organization Positions (Authoritative Source — Webflow CMS)"]

    if result.supporting_orgs:
        lines = ["### Organizations Supporting This Bill"]
        for org in result.supporting_orgs:
            if org.slug:
                line = f"- [{org.name}](https://digitaldemocracyproject.org/member-organizations/{org.slug})"
            else:
                line = f"- {org.name}"
            if org.org_type:
                line += f" ({org.org_type})"
            lines.append(line)
        parts.append("\n".join(lines))

    if result.opposing_orgs:
        lines = ["### Organizations Opposing This Bill"]
        for org in result.opposing_orgs:
            if org.slug:
                line = f"- [{org.name}](https://digitaldemocracyproject.org/member-organizations/{org.slug})"
            else:
                line = f"- {org.name}"
            if org.org_type:
                line += f" ({org.org_type})"
            lines.append(line)
        parts.append("\n".join(lines))

    parts.append(
        "*This information is fetched directly from the Digital Democracy Project CMS "
        "and should be considered authoritative.*"
    )

    return "\n\n".join(parts)


def format_org_bill_positions_context(result: OrgBillPositionsResult) -> str:
    """
    Format an org's bill positions as markdown for LLM context injection.

    Mirrors format_org_positions_context but for the reverse direction:
    given an org, list the bills it supports/opposes.

    Args:
        result: OrgBillPositionsResult from get_org_bill_positions

    Returns:
        Formatted markdown string, or empty string if no data
    """
    if not result.found:
        return ""

    if not result.supported_bills and not result.opposed_bills:
        return (
            f"## Bill Positions for {result.org_name} (Authoritative Source — Webflow CMS)\n\n"
            f"No bill positions have been recorded for this organization in the Digital Democracy Project database."
        )

    parts = [f"## Bill Positions for {result.org_name} (Authoritative Source — Webflow CMS)"]

    if result.supported_bills:
        lines = ["### Bills Supported"]
        for bill in result.supported_bills:
            if bill.slug:
                line = f"- [{bill.name}](https://digitaldemocracyproject.org/bills/{bill.slug})"
            else:
                line = f"- {bill.name}"
            if bill.bill_id:
                line += f" ({bill.bill_id})"
            lines.append(line)
        parts.append("\n".join(lines))

    if result.opposed_bills:
        lines = ["### Bills Opposed"]
        for bill in result.opposed_bills:
            if bill.slug:
                line = f"- [{bill.name}](https://digitaldemocracyproject.org/bills/{bill.slug})"
            else:
                line = f"- {bill.name}"
            if bill.bill_id:
                line += f" ({bill.bill_id})"
            lines.append(line)
        parts.append("\n".join(lines))

    parts.append(
        "*This information is fetched directly from the Digital Democracy Project CMS "
        "and should be considered authoritative.*"
    )

    return "\n\n".join(parts)
