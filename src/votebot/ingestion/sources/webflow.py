"""Webflow CMS data source connector."""

from typing import AsyncIterator

import httpx
import structlog
from bs4 import BeautifulSoup

from votebot.config import Settings, get_settings
from votebot.ingestion.metadata import DocumentMetadata, MetadataExtractor
from votebot.ingestion.pipeline import DocumentSource
from votebot.ingestion.sources.pdf import PDFSource

logger = structlog.get_logger()


class WebflowSource:
    """
    Data source connector for Webflow CMS.

    Fetches DDP content including:
    - Bill pages with PDF text extraction
    - Educational materials
    - Legislator profiles
    - General content
    """

    BASE_URL = "https://api.webflow.com/v2"

    def __init__(
        self,
        settings: Settings | None = None,
        metadata_extractor: MetadataExtractor | None = None,
    ):
        """
        Initialize the Webflow source.

        Args:
            settings: Application settings
            metadata_extractor: Metadata extractor instance
        """
        self.settings = settings or get_settings()
        self.metadata_extractor = metadata_extractor or MetadataExtractor()
        self.api_key = self.settings.webflow_api_key.get_secret_value()
        self.site_id = self.settings.webflow_site_id
        self.bills_collection_id = self.settings.webflow_bills_collection_id
        self.legislators_collection_id = self.settings.webflow_legislators_collection_id
        self.jurisdiction_collection_id = self.settings.webflow_jurisdiction_collection_id
        self.organizations_collection_id = self.settings.webflow_organizations_collection_id
        self.pdf_source = PDFSource(self.settings, self.metadata_extractor)
        self._jurisdiction_cache: dict[str, str] = {}
        self._bill_cache: dict[str, dict] = {}  # bill_id -> {name, identifier}
        self._organization_cache: dict[str, dict] = {}  # org_id -> {name, type}

    async def fetch(
        self,
        collection_id: str | None = None,
        limit: int = 0,
        include_pdfs: bool = True,
        **kwargs,
    ) -> AsyncIterator[DocumentSource]:
        """
        Fetch content from Webflow CMS.

        Args:
            collection_id: Optional collection ID (defaults to bills collection)
            limit: Maximum number of items to fetch (0 = unlimited)
            include_pdfs: Whether to download and process gov-url PDFs

        Yields:
            DocumentSource objects for each content item
        """
        coll_id = collection_id or self.bills_collection_id

        if not coll_id:
            logger.error("No collection ID provided and no bills collection configured")
            return

        async with httpx.AsyncClient(timeout=60.0) as client:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "accept": "application/json",
            }

            # Build organization mapping for bill enrichment (if fetching bills)
            if coll_id == self.bills_collection_id:
                await self._build_organization_mapping(client, headers)

            logger.info(
                "Fetching from Webflow collection",
                collection_id=coll_id,
                limit=limit if limit > 0 else "unlimited",
            )

            # Pagination variables
            offset = 0
            page_size = 100  # Webflow API max per request
            total_fetched = 0
            all_items = []

            # Fetch all pages
            while True:
                try:
                    params = {"limit": page_size, "offset": offset}
                    response = await client.get(
                        f"{self.BASE_URL}/collections/{coll_id}/items",
                        headers=headers,
                        params=params,
                    )
                    response.raise_for_status()
                    data = response.json()
                    items = data.get("items", [])
                    pagination = data.get("pagination", {})

                    if not items:
                        break

                    all_items.extend(items)
                    total_fetched += len(items)

                    logger.info(
                        f"Fetched page {offset // page_size + 1}: {len(items)} items "
                        f"(total: {total_fetched})"
                    )

                    # Check if we've hit the limit
                    if limit > 0 and total_fetched >= limit:
                        all_items = all_items[:limit]
                        break

                    # Check if there are more pages
                    total_in_collection = pagination.get("total", 0)
                    if total_fetched >= total_in_collection or len(items) < page_size:
                        break

                    offset += page_size

                except Exception as e:
                    logger.error(
                        "Failed to fetch Webflow collection items",
                        collection_id=coll_id,
                        offset=offset,
                        error=str(e),
                    )
                    break

            logger.info(f"Total items fetched: {len(all_items)}")

            for item in all_items:
                try:
                    async for doc in self._process_bill_item(item, include_pdfs):
                        yield doc
                except Exception as e:
                    logger.warning(
                        "Failed to process Webflow item",
                        item_id=item.get("id"),
                        error=str(e),
                    )
                    continue

    async def fetch_bills(
        self,
        limit: int = 100,
        include_pdfs: bool = True,
    ) -> AsyncIterator[DocumentSource]:
        """
        Fetch bills from the configured bills collection.

        Args:
            limit: Maximum number of bills to fetch
            include_pdfs: Whether to download and process gov-url PDFs

        Yields:
            DocumentSource objects for each bill
        """
        async for doc in self.fetch(
            collection_id=self.bills_collection_id,
            limit=limit,
            include_pdfs=include_pdfs,
        ):
            yield doc

    async def fetch_legislators(
        self,
        limit: int = 0,
    ) -> AsyncIterator[DocumentSource]:
        """
        Fetch legislators from Webflow CMS.

        Uses WEBFLOW_LEGISLATORS_COLLECTION_ID to fetch DDP-curated legislators
        with their scores and scorecards. Resolves jurisdiction reference IDs
        to state codes.

        Args:
            limit: Maximum number of legislators to fetch (0 = unlimited)

        Yields:
            DocumentSource objects for each legislator
        """
        if not self.legislators_collection_id:
            logger.error("No legislators collection ID configured")
            return

        async with httpx.AsyncClient(timeout=60.0) as client:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "accept": "application/json",
            }

            # Build jurisdiction mapping first
            await self._build_jurisdiction_mapping(client, headers)

            logger.info(
                "Fetching legislators from Webflow",
                collection_id=self.legislators_collection_id,
                limit=limit if limit > 0 else "unlimited",
            )

            # Pagination variables
            offset = 0
            page_size = 100
            total_fetched = 0
            all_items = []

            # Fetch all pages
            while True:
                try:
                    params = {"limit": page_size, "offset": offset}
                    response = await client.get(
                        f"{self.BASE_URL}/collections/{self.legislators_collection_id}/items",
                        headers=headers,
                        params=params,
                    )
                    response.raise_for_status()
                    data = response.json()
                    items = data.get("items", [])
                    pagination = data.get("pagination", {})

                    if not items:
                        break

                    all_items.extend(items)
                    total_fetched += len(items)

                    logger.info(
                        f"Fetched legislators page {offset // page_size + 1}: "
                        f"{len(items)} items (total: {total_fetched})"
                    )

                    # Check if we've hit the limit
                    if limit > 0 and total_fetched >= limit:
                        all_items = all_items[:limit]
                        break

                    # Check if there are more pages
                    total_in_collection = pagination.get("total", 0)
                    if total_fetched >= total_in_collection or len(items) < page_size:
                        break

                    offset += page_size

                except Exception as e:
                    logger.error(
                        "Failed to fetch legislators",
                        collection_id=self.legislators_collection_id,
                        offset=offset,
                        error=str(e),
                    )
                    break

            logger.info(f"Total legislators fetched: {len(all_items)}")

            for item in all_items:
                try:
                    doc = self._process_legislator_item(item)
                    if doc:
                        yield doc
                except Exception as e:
                    logger.warning(
                        "Failed to process legislator item",
                        item_id=item.get("id"),
                        error=str(e),
                    )
                    continue

    async def fetch_organizations(
        self,
        limit: int = 0,
    ) -> AsyncIterator[DocumentSource]:
        """
        Fetch member organizations from Webflow CMS.

        Resolves bill references (support/oppose) to bill names for readable content.
        Organizations do not have strict jurisdiction metadata.

        Args:
            limit: Maximum number of organizations to fetch (0 = unlimited)

        Yields:
            DocumentSource objects for each organization
        """
        if not self.organizations_collection_id:
            logger.error("No organizations collection ID configured")
            return

        async with httpx.AsyncClient(timeout=60.0) as client:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "accept": "application/json",
            }

            # Build bill mapping first (for resolving bill references)
            await self._build_bill_mapping(client, headers)

            logger.info(
                "Fetching organizations from Webflow",
                collection_id=self.organizations_collection_id,
                limit=limit if limit > 0 else "unlimited",
            )

            # Pagination variables
            offset = 0
            page_size = 100
            total_fetched = 0
            all_items = []

            # Fetch all pages
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
                    pagination = data.get("pagination", {})

                    if not items:
                        break

                    all_items.extend(items)
                    total_fetched += len(items)

                    logger.info(
                        f"Fetched organizations page {offset // page_size + 1}: "
                        f"{len(items)} items (total: {total_fetched})"
                    )

                    # Check if we've hit the limit
                    if limit > 0 and total_fetched >= limit:
                        all_items = all_items[:limit]
                        break

                    # Check if there are more pages
                    total_in_collection = pagination.get("total", 0)
                    if total_fetched >= total_in_collection or len(items) < page_size:
                        break

                    offset += page_size

                except Exception as e:
                    logger.error(
                        "Failed to fetch organizations",
                        collection_id=self.organizations_collection_id,
                        offset=offset,
                        error=str(e),
                    )
                    break

            logger.info(f"Total organizations fetched: {len(all_items)}")

            for item in all_items:
                try:
                    doc = await self._process_organization_item(item)
                    if doc:
                        yield doc
                except Exception as e:
                    logger.warning(
                        "Failed to process organization item",
                        item_id=item.get("id"),
                        error=str(e),
                    )
                    continue

    async def _build_bill_mapping(
        self,
        client: httpx.AsyncClient,
        headers: dict,
    ) -> None:
        """
        Build a mapping from bill IDs to bill names.

        Fetches the bills collection and creates a lookup table for resolving
        bill references in organizations.
        """
        if self._bill_cache:
            return  # Already cached

        if not self.bills_collection_id:
            logger.warning("No bills collection ID configured")
            return

        logger.info("Building bill mapping for organization references...")

        try:
            offset = 0
            page_size = 100

            while True:
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
                    item_id = item.get("id", "")
                    fields = item.get("fieldData", {})
                    name = fields.get("name", "")
                    identifier = f"{fields.get('bill-prefix', '')} {fields.get('bill-number', '')}".strip()
                    slug = fields.get("slug", "")

                    if item_id:
                        self._bill_cache[item_id] = {
                            "name": name,
                            "identifier": identifier if identifier else name,
                            "slug": slug,  # Include slug for DDP URL generation
                        }

                pagination = data.get("pagination", {})
                total = pagination.get("total", 0)
                if len(self._bill_cache) >= total or len(items) < page_size:
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

    async def _build_organization_mapping(
        self,
        client: httpx.AsyncClient,
        headers: dict,
    ) -> None:
        """
        Build a mapping from organization IDs to organization names.

        Fetches the organizations collection and creates a lookup table for resolving
        organization references in bills.
        """
        if self._organization_cache:
            return  # Already cached

        if not self.organizations_collection_id:
            logger.warning("No organizations collection ID configured")
            return

        logger.info("Building organization mapping for bill references...")

        try:
            offset = 0
            page_size = 100

            while True:
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
                    item_id = item.get("id", "")
                    fields = item.get("fieldData", {})
                    name = fields.get("name", "")
                    org_type = fields.get("type-2", "")
                    slug = fields.get("slug", "")

                    if item_id and name:
                        self._organization_cache[item_id] = {
                            "name": name,
                            "type": org_type,
                            "slug": slug,
                        }

                pagination = data.get("pagination", {})
                total = pagination.get("total", 0)
                if len(self._organization_cache) >= total or len(items) < page_size:
                    break

                offset += page_size

            logger.info(
                f"Built organization mapping with {len(self._organization_cache)} entries"
            )

        except Exception as e:
            logger.warning(
                "Failed to build organization mapping",
                error=str(e),
            )

    async def _resolve_organization_references(self, org_refs: list | None) -> list[dict]:
        """
        Resolve organization reference IDs to organization information.

        Fetches missing organizations directly from Webflow API and adds them
        to the cache for future use.

        Args:
            org_refs: List of organization reference IDs from Webflow

        Returns:
            List of dicts with organization name and type
        """
        if not org_refs:
            return []

        resolved = []
        for ref in org_refs:
            if isinstance(ref, str):
                org_info = self._organization_cache.get(ref)
                if org_info:
                    resolved.append(org_info)
                else:
                    # Fetch missing organization from Webflow API
                    fetched = await self._fetch_organization_by_id(ref)
                    if fetched:
                        resolved.append(fetched)
                    else:
                        # Still not found, use ID as fallback
                        logger.warning(
                            "Organization not found",
                            org_id=ref,
                        )
                        resolved.append({"name": f"Organization {ref[:8]}...", "type": "", "slug": ""})
            elif isinstance(ref, dict):
                # Already resolved
                resolved.append(ref)

        return resolved

    async def _fetch_organization_by_id(self, org_id: str) -> dict | None:
        """
        Fetch a single organization from Webflow and add to cache.

        Args:
            org_id: Webflow organization item ID

        Returns:
            Organization info dict or None if not found
        """
        if not self.organizations_collection_id:
            return None

        try:
            item = await self.fetch_item_by_id(self.organizations_collection_id, org_id)
            if not item:
                return None

            fields = item.get("fieldData", {})
            name = fields.get("name", "")
            org_type = fields.get("type-2", "")
            slug = fields.get("slug", "")

            if name:
                org_info = {
                    "name": name,
                    "type": org_type,
                    "slug": slug,
                }
                # Add to cache for future use
                self._organization_cache[org_id] = org_info
                logger.info(
                    "Fetched and cached organization",
                    org_id=org_id,
                    name=name,
                )
                return org_info

        except Exception as e:
            logger.warning(
                "Failed to fetch organization",
                org_id=org_id,
                error=str(e),
            )

        return None

    async def _process_organization_item(self, item: dict) -> DocumentSource | None:
        """
        Process a single organization item from the CMS.

        Args:
            item: Webflow CMS item data

        Returns:
            DocumentSource object or None if processing fails
        """
        fields = item.get("fieldData", {})
        item_id = item.get("id", "")
        name = fields.get("name", "Unknown Organization")

        if not name or name == "Unknown Organization":
            logger.debug(f"Organization has no name, skipping: {item_id}")
            return None

        logger.debug(f"Processing organization: {name}")

        # Resolve bill references
        bills_support = await self._resolve_bill_references(fields.get("bills-support", []))
        bills_oppose = await self._resolve_bill_references(fields.get("bills-oppose", []))

        # Extract content
        content = self._extract_organization_content(fields, bills_support, bills_oppose)

        if not content:
            logger.debug(f"No content extracted for organization {name}")
            return None

        # Create metadata with DDP URL for citation linking
        slug = fields.get("slug", "")
        ddp_url = f"https://digitaldemocracyproject.org/member-organizations/{slug}" if slug else None

        metadata = DocumentMetadata(
            document_id=f"organization-{item_id}",
            document_type="organization",
            source="Digital Democracy Project",
            title=name,
            jurisdiction=None,  # Organizations can be local, state, or national
            url=ddp_url,
            extra={
                "webflow_id": item_id,
                "slug": slug,  # DDP URL slug for linking
                "organization_type": fields.get("type-2", ""),
                "website": fields.get("website", ""),
                "bills_support_count": len(bills_support),
                "bills_oppose_count": len(bills_oppose),
            },
        )

        return DocumentSource(
            content=content,
            metadata=metadata,
        )

    async def _resolve_bill_references(self, bill_refs: list | None) -> list[dict]:
        """
        Resolve bill reference IDs to bill information.

        Fetches missing bills directly from Webflow API and adds them
        to the cache for future use.

        Args:
            bill_refs: List of bill reference IDs from Webflow

        Returns:
            List of dicts with bill name and identifier
        """
        if not bill_refs:
            return []

        resolved = []
        for ref in bill_refs:
            if isinstance(ref, str):
                bill_info = self._bill_cache.get(ref)
                if bill_info:
                    resolved.append(bill_info)
                else:
                    # Fetch missing bill from Webflow API
                    fetched = await self._fetch_bill_by_id(ref)
                    if fetched:
                        resolved.append(fetched)
                    else:
                        # Still not found, use ID as fallback
                        logger.warning(
                            "Bill not found",
                            bill_id=ref,
                        )
                        resolved.append({"name": f"Bill {ref[:8]}...", "identifier": ref, "slug": ""})
            elif isinstance(ref, dict):
                # Already resolved
                resolved.append(ref)

        return resolved

    async def _fetch_bill_by_id(self, bill_id: str) -> dict | None:
        """
        Fetch a single bill from Webflow and add to cache.

        Args:
            bill_id: Webflow bill item ID

        Returns:
            Bill info dict or None if not found
        """
        if not self.bills_collection_id:
            return None

        try:
            item = await self.fetch_item_by_id(self.bills_collection_id, bill_id)
            if not item:
                return None

            fields = item.get("fieldData", {})
            name = fields.get("name", "")
            slug = fields.get("slug", "")
            identifier = f"{fields.get('bill-prefix', '')} {fields.get('bill-number', '')}".strip()

            if name or identifier:
                bill_info = {
                    "name": name,
                    "identifier": identifier if identifier else name,
                    "slug": slug,
                }
                # Add to cache for future use
                self._bill_cache[bill_id] = bill_info
                logger.info(
                    "Fetched and cached bill",
                    bill_id=bill_id,
                    name=name,
                )
                return bill_info

        except Exception as e:
            logger.warning(
                "Failed to fetch bill",
                bill_id=bill_id,
                error=str(e),
            )

        return None

    def _extract_organization_content(
        self,
        fields: dict,
        bills_support: list[dict],
        bills_oppose: list[dict],
    ) -> str:
        """
        Extract text content from organization CMS fields.

        Args:
            fields: CMS field data
            bills_support: Resolved bills the organization supports
            bills_oppose: Resolved bills the organization opposes

        Returns:
            Formatted text content for embedding
        """
        parts = []

        # Name/Title
        name = fields.get("name", "")
        if name:
            parts.append(f"# {name}")

        # Organization type
        org_type = fields.get("type-2", "")
        if org_type:
            parts.append(f"**Type:** {org_type}")

        # Website
        website = fields.get("website", "")
        if website:
            parts.append(f"**Website:** {website}")

        # About section
        about = fields.get("about-organization", "")
        if about:
            about_text = self._html_to_text(about) if "<" in about else about
            if about_text:
                parts.append(f"## About\n{about_text}")

        # Extended description
        description = fields.get("description-4", "")
        if description:
            desc_text = self._html_to_text(description) if "<" in description else description
            if desc_text and desc_text != about:  # Avoid duplicating content
                parts.append(f"## Description\n{desc_text}")

        # Policy positions
        policies = fields.get("policies-2", "")
        if policies:
            policy_text = self._html_to_text(policies) if "<" in policies else policies
            if policy_text:
                parts.append(f"## Policy Positions\n{policy_text}")

        # Funding
        funding = fields.get("funding-2", "")
        if funding:
            funding_text = self._html_to_text(funding) if "<" in funding else funding
            if funding_text:
                parts.append(f"## Funding\n{funding_text}")

        # Affiliates
        affiliates = fields.get("affiliates-2", "")
        if affiliates:
            affiliates_text = self._html_to_text(affiliates) if "<" in affiliates else affiliates
            if affiliates_text:
                parts.append(f"## Affiliates\n{affiliates_text}")

        # Bill positions section with DDP URLs
        if bills_support or bills_oppose:
            parts.append("## Bill Positions")

            if bills_support:
                support_lines = ["### Bills Supported"]
                for bill in bills_support:
                    identifier = bill.get("identifier", "")
                    bill_name = bill.get("name", "")
                    slug = bill.get("slug", "")
                    # Include DDP URL if slug is available
                    if slug:
                        ddp_url = f"https://digitaldemocracyproject.org/bills/{slug}"
                        if bill_name:
                            support_lines.append(f"- [{bill_name}]({ddp_url})")
                        elif identifier:
                            support_lines.append(f"- [{identifier}]({ddp_url})")
                    elif identifier and bill_name:
                        support_lines.append(f"- {bill_name} ({identifier})")
                    elif bill_name:
                        support_lines.append(f"- {bill_name}")
                    elif identifier:
                        support_lines.append(f"- {identifier}")
                parts.append("\n".join(support_lines))

            if bills_oppose:
                oppose_lines = ["### Bills Opposed"]
                for bill in bills_oppose:
                    identifier = bill.get("identifier", "")
                    bill_name = bill.get("name", "")
                    slug = bill.get("slug", "")
                    # Include DDP URL if slug is available
                    if slug:
                        ddp_url = f"https://digitaldemocracyproject.org/bills/{slug}"
                        if bill_name:
                            oppose_lines.append(f"- [{bill_name}]({ddp_url})")
                        elif identifier:
                            oppose_lines.append(f"- [{identifier}]({ddp_url})")
                    elif identifier and bill_name:
                        oppose_lines.append(f"- {bill_name} ({identifier})")
                    elif bill_name:
                        oppose_lines.append(f"- {bill_name}")
                    elif identifier:
                        oppose_lines.append(f"- {identifier}")
                parts.append("\n".join(oppose_lines))

        return "\n\n".join(parts) if parts else ""

    async def _build_jurisdiction_mapping(
        self,
        client: httpx.AsyncClient,
        headers: dict,
    ) -> None:
        """
        Build a mapping from jurisdiction reference IDs to state codes.

        Fetches the jurisdictions collection and creates a lookup table.
        """
        if self._jurisdiction_cache:
            return  # Already cached

        if not self.jurisdiction_collection_id:
            logger.warning("No jurisdiction collection ID configured")
            return

        try:
            offset = 0
            page_size = 100

            while True:
                params = {"limit": page_size, "offset": offset}
                response = await client.get(
                    f"{self.BASE_URL}/collections/{self.jurisdiction_collection_id}/items",
                    headers=headers,
                    params=params,
                )
                response.raise_for_status()
                data = response.json()
                items = data.get("items", [])

                if not items:
                    break

                for item in items:
                    item_id = item.get("id", "")
                    fields = item.get("fieldData", {})
                    # Try common field names for state code
                    state_code = (
                        fields.get("state-code")
                        or fields.get("code")
                        or fields.get("abbreviation")
                        or fields.get("name", "")[:2].upper()
                    )
                    if item_id and state_code:
                        self._jurisdiction_cache[item_id] = state_code

                pagination = data.get("pagination", {})
                total = pagination.get("total", 0)
                if len(self._jurisdiction_cache) >= total or len(items) < page_size:
                    break

                offset += page_size

            logger.info(
                f"Built jurisdiction mapping with {len(self._jurisdiction_cache)} entries"
            )

        except Exception as e:
            logger.warning(
                "Failed to build jurisdiction mapping",
                error=str(e),
            )

    def _process_legislator_item(self, item: dict) -> DocumentSource | None:
        """
        Process a single legislator item from the CMS.

        Args:
            item: Webflow CMS item data

        Returns:
            DocumentSource object or None if processing fails
        """
        fields = item.get("fieldData", {})
        item_id = item.get("id", "")
        name = fields.get("name", "Unknown Legislator")

        # Get OpenStates ID (critical for linking)
        openstates_id = fields.get("openstatesid", "")
        if not openstates_id:
            logger.debug(f"Legislator {name} has no openstatesid, skipping")
            return None

        logger.debug(f"Processing legislator: {name}")

        # Resolve jurisdiction reference to state code
        jurisdiction_ref = fields.get("jurisdiction")
        state_code = self._resolve_jurisdiction(jurisdiction_ref)

        # Extract content from post-body (DDP scorecards)
        content = self._extract_legislator_content(fields)

        if not content:
            logger.debug(f"No content extracted for legislator {name}")
            return None

        # Create metadata with DDP URL for citation linking
        slug = fields.get("slug", "")
        ddp_url = f"https://digitaldemocracyproject.org/legislators/{slug}" if slug else None

        metadata = DocumentMetadata(
            document_id=f"legislator-{openstates_id}",
            document_type="legislator",
            source="Digital Democracy Project",
            title=name,
            jurisdiction=state_code,
            legislator_id=openstates_id,
            url=ddp_url,
            extra={
                "webflow_id": item_id,
                "slug": slug,  # DDP URL slug for linking
                "party": fields.get("party-2", fields.get("party", "")),
                "chamber": fields.get("chamber", ""),
                "district": fields.get("district", ""),
                "ddp_score": fields.get("score", ""),
                "email": fields.get("email", ""),
                "image_url": fields.get("image", {}).get("url", "") if isinstance(fields.get("image"), dict) else fields.get("image", ""),
            },
        )

        return DocumentSource(
            content=content,
            metadata=metadata,
        )

    def _resolve_jurisdiction(self, jurisdiction_ref: str | list | None) -> str:
        """
        Resolve a jurisdiction reference ID to a state code.

        Args:
            jurisdiction_ref: Reference ID, list of IDs, or state code string

        Returns:
            State code (e.g., "FL", "WA") or "US" if not found
        """
        if not jurisdiction_ref:
            return "US"

        # Handle list of references
        if isinstance(jurisdiction_ref, list):
            if jurisdiction_ref:
                jurisdiction_ref = jurisdiction_ref[0]
            else:
                return "US"

        # Already a 2-letter state code
        if isinstance(jurisdiction_ref, str) and len(jurisdiction_ref) == 2:
            return jurisdiction_ref.upper()

        # Look up in cache
        if isinstance(jurisdiction_ref, str):
            return self._jurisdiction_cache.get(jurisdiction_ref, "US")

        return "US"

    def _extract_legislator_content(self, fields: dict) -> str:
        """
        Extract text content from legislator CMS fields.

        Args:
            fields: CMS field data

        Returns:
            Formatted text content for embedding
        """
        parts = []

        # Name/Title
        name = fields.get("name", "")
        if name:
            parts.append(f"# {name}")

        # Basic info section
        info_parts = []
        party = fields.get("party-2", fields.get("party", ""))
        if party:
            info_parts.append(f"**Party:** {party}")

        chamber = fields.get("chamber", "")
        if chamber:
            info_parts.append(f"**Chamber:** {chamber}")

        district = fields.get("district", "")
        if district:
            info_parts.append(f"**District:** {district}")

        score = fields.get("score")
        if score is not None and score != "":
            info_parts.append(f"**DDP Accountability Score:** {score}")

        if info_parts:
            parts.append("\n".join(info_parts))

        # DDP Scorecard content (from post-body)
        post_body = fields.get("post-body", "")
        if post_body:
            scorecard_text = self._html_to_text(post_body)
            if scorecard_text:
                parts.append(f"## DDP Scorecard and Voting Record\n{scorecard_text}")

        # Description/bio
        description = fields.get("description", "")
        if description:
            desc_text = self._html_to_text(description) if "<" in description else description
            if desc_text:
                parts.append(f"## About\n{desc_text}")

        # Contact info
        contact_parts = []
        email = fields.get("email", "")
        if email:
            contact_parts.append(f"- Email: {email}")

        phone = fields.get("phone", "")
        if phone:
            contact_parts.append(f"- Phone: {phone}")

        if contact_parts:
            parts.append("## Contact Information\n" + "\n".join(contact_parts))

        return "\n\n".join(parts) if parts else ""

    async def _process_bill_item(
        self,
        item: dict,
        include_pdfs: bool = True,
    ) -> AsyncIterator[DocumentSource]:
        """
        Process a single bill item from the CMS.

        Args:
            item: Webflow CMS item data
            include_pdfs: Whether to download and process the gov-url PDF

        Yields:
            DocumentSource objects (CMS content and optionally PDF content)
        """
        fields = item.get("fieldData", {})
        item_id = item.get("id", "")
        name = fields.get("name", "Unknown Bill")

        logger.info(f"Processing bill: {name}")

        # Resolve organization references for bill positions
        supporting_orgs = await self._resolve_organization_references(
            fields.get("member-organizations", [])
        )
        opposing_orgs = await self._resolve_organization_references(
            fields.get("organizations-oppose", [])
        )

        # Extract CMS content with organization positions
        cms_content = self._extract_bill_content(
            fields,
            supporting_orgs=supporting_orgs,
            opposing_orgs=opposing_orgs,
        )

        # Create enhanced metadata with DDP URL for citation linking
        slug = fields.get("slug", "")
        ddp_url = f"https://digitaldemocracyproject.org/bills/{slug}" if slug else None

        metadata = DocumentMetadata(
            document_id=f"bill-webflow-{item_id}",
            document_type="bill",
            source="Digital Democracy Project",
            title=name,
            jurisdiction=self._get_jurisdiction(fields),
            bill_id=self._get_bill_id(fields),
            url=ddp_url,  # Use DDP URL for citation linking
            extra={
                "webflow_id": item_id,
                # Bill identification
                "slug": slug,  # DDP citation URL
                "session_code": fields.get("session-code", ""),
                "bill_prefix": fields.get("bill-prefix", ""),
                "bill_number": fields.get("bill-number", ""),
                "status": fields.get("status", ""),
                "session": fields.get("bill-session", ""),
                # External links
                "gov_url": fields.get("gov-url", ""),
                "open_plural_url": fields.get("open-plural-url", ""),
                "kialo_url": fields.get("kialo-url", ""),
                # Organization positions
                "supporting_orgs_count": len(supporting_orgs),
                "opposing_orgs_count": len(opposing_orgs),
            },
        )

        # Yield CMS content first
        if cms_content:
            yield DocumentSource(
                content=cms_content,
                metadata=metadata,
            )

        # Process PDF if gov-url is available
        # Check for PDF URLs by extension, path patterns, or content-type
        gov_url = fields.get("gov-url")
        if include_pdfs and gov_url:
            is_pdf_url = await self._is_pdf_url(gov_url)
            logger.info(
                "PDF processing check",
                include_pdfs=include_pdfs,
                gov_url=gov_url,
                is_pdf_url=is_pdf_url,
            )
            if is_pdf_url:
                pdf_doc = await self._process_bill_pdf(gov_url, fields, item_id)
                if pdf_doc:
                    yield pdf_doc

    def _get_source_from_url(self, url: str) -> str:
        """
        Extract a human-readable source name from a government URL.

        Args:
            url: The government website URL (e.g., https://www.congress.gov/...)

        Returns:
            Human-readable source name (e.g., "Congress.gov", "Florida Senate")
        """
        from urllib.parse import urlparse

        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()

            # Remove www. prefix
            if domain.startswith("www."):
                domain = domain[4:]

            # Map known domains to friendly names
            domain_map = {
                "congress.gov": "Congress.gov",
                "flsenate.gov": "Florida Senate",
                "flhouse.gov": "Florida House",
                "myfloridahouse.gov": "Florida House",
                "leg.wa.gov": "Washington Legislature",
                "apps.leg.wa.gov": "Washington Legislature",
                "legislature.mi.gov": "Michigan Legislature",
                "le.utah.gov": "Utah Legislature",
                "azleg.gov": "Arizona Legislature",
                "malegislature.gov": "Massachusetts Legislature",
                "lis.virginia.gov": "Virginia Legislature",
                "virginiageneralassembly.gov": "Virginia Legislature",
            }

            # Check for exact match
            if domain in domain_map:
                return domain_map[domain]

            # Check for partial match (e.g., "apps.leg.wa.gov" contains "leg.wa.gov")
            for key, value in domain_map.items():
                if key in domain or domain.endswith(key):
                    return value

            # Fallback: use the domain as-is, capitalized nicely
            return domain.replace(".", " ").title().replace(" Gov", ".gov")

        except Exception:
            return "Government Source"

    async def _is_pdf_url(self, url: str) -> bool:
        """
        Check if a URL points to a PDF document.

        Checks:
        1. URL extension patterns (.pdf, /pdf)
        2. Known legislative bill text URL patterns
        3. Content-Type header via HEAD request

        Args:
            url: The URL to check

        Returns:
            True if the URL likely points to a PDF
        """
        url_lower = url.lower()

        # Check URL extension patterns
        if url_lower.endswith(".pdf") or url_lower.endswith("/pdf"):
            return True

        # Check known legislative text URL patterns that serve PDFs
        pdf_path_patterns = [
            "/text/",        # Virginia: /bill-details/20261/SB1/text/SB1
            "/billtext/",    # Florida: /Session/Bill/2026/363/BillText/Filed/PDF
            "/bill/text",    # Generic pattern
            "/fulltext",     # Some states
            "/document/",    # Document endpoints often serve PDFs
        ]
        for pattern in pdf_path_patterns:
            if pattern in url_lower:
                logger.debug(f"URL matches PDF path pattern: {pattern}")
                return True

        # Fall back to HEAD request to check Content-Type
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.head(url)
                content_type = response.headers.get("content-type", "").lower()
                if "application/pdf" in content_type:
                    logger.debug(f"URL returns PDF content-type: {content_type}")
                    return True
        except Exception as e:
            logger.debug(f"HEAD request failed for PDF check: {e}")

        return False

    async def _process_bill_pdf(
        self,
        pdf_url: str,
        fields: dict,
        item_id: str,
    ) -> DocumentSource | None:
        """
        Download and process a bill PDF.

        Args:
            pdf_url: URL to the PDF
            fields: CMS field data
            item_id: Webflow item ID

        Returns:
            DocumentSource with PDF content or None
        """
        logger.info(f"Downloading PDF: {pdf_url}")

        try:
            doc = await self.pdf_source.process_url(pdf_url)
            if not doc:
                return None

            # Create metadata for PDF content
            name = fields.get("name", "Unknown Bill")
            slug = fields.get("slug", "")
            bill_id = self._get_bill_id(fields)

            # Derive source name from the government URL
            source_name = self._get_source_from_url(pdf_url)

            # Build DDP URL for the bill page
            ddp_url = f"https://digitaldemocracyproject.org/bills/{slug}" if slug else None

            # Prepend header with bill identification and DDP link
            header_parts = []
            if ddp_url:
                header_parts.append(f"# [{name}]({ddp_url})")
                header_parts.append(f"**Bill:** [{bill_id}]({ddp_url})")
            else:
                header_parts.append(f"# {name}")
                header_parts.append(f"**Bill:** {bill_id}")
            header_parts.append(f"**Source:** [Official Legislative Text]({pdf_url})")
            header_parts.append("\n---\n")

            # Combine header with PDF content
            content_with_header = "\n".join(header_parts) + doc.content

            metadata = DocumentMetadata(
                document_id=f"bill-pdf-{item_id}",
                document_type="bill-text",
                source=source_name,
                title=f"{name} - Full Text",
                jurisdiction=self._get_jurisdiction(fields),
                bill_id=bill_id,
                url=pdf_url,
                extra={
                    "webflow_id": item_id,
                    "slug": slug,  # Include slug for DDP linking
                    "content_type": "pdf",
                    "session": fields.get("bill-session"),
                    "bill_prefix": fields.get("bill-prefix"),
                    "bill_number": fields.get("bill-number"),
                },
            )

            return DocumentSource(
                content=content_with_header,
                metadata=metadata,
            )

        except Exception as e:
            logger.warning(
                "Failed to process bill PDF",
                url=pdf_url,
                error=str(e),
            )
            return None

    def _extract_bill_content(
        self,
        fields: dict,
        supporting_orgs: list[dict] | None = None,
        opposing_orgs: list[dict] | None = None,
    ) -> str:
        """
        Extract text content from bill CMS fields with organization positions.

        Args:
            fields: CMS field data
            supporting_orgs: Resolved organizations that support this bill
            opposing_orgs: Resolved organizations that oppose this bill

        Returns:
            Formatted text content for embedding
        """
        parts = []

        # Name/Title
        if fields.get("name"):
            parts.append(f"# {fields['name']}")

        # Bill identification info
        info_parts = []
        if fields.get("bill-prefix") and fields.get("bill-number"):
            info_parts.append(f"**Bill Number:** {fields['bill-prefix']} {fields['bill-number']}")
        if fields.get("session-code"):
            info_parts.append(f"**Session:** {fields['session-code']}")
        elif fields.get("bill-session"):
            info_parts.append(f"**Session:** {fields['bill-session']}")
        if fields.get("status"):
            info_parts.append(f"**Status:** {fields['status']}")
        if info_parts:
            parts.append("\n".join(info_parts))

        # Description (full text, not truncated)
        if fields.get("description"):
            desc_text = fields["description"]
            if "<" in desc_text:
                desc_text = self._html_to_text(desc_text)
            if desc_text:
                parts.append(f"## Description\n{desc_text}")

        # Support arguments
        if fields.get("support"):
            support_text = self._html_to_text(fields["support"])
            if support_text:
                parts.append(f"## Arguments in Support\n{support_text}")

        # Opposition arguments
        if fields.get("oppose"):
            oppose_text = self._html_to_text(fields["oppose"])
            if oppose_text:
                parts.append(f"## Arguments in Opposition\n{oppose_text}")

        # Post body (main content/details)
        if fields.get("post-body"):
            body_text = self._html_to_text(fields["post-body"])
            if body_text:
                parts.append(f"## Details\n{body_text}")

        # Organization positions section
        if supporting_orgs or opposing_orgs:
            parts.append("## Organization Positions")

            if supporting_orgs:
                support_lines = ["### Organizations Supporting This Bill"]
                for org in supporting_orgs:
                    org_name = org.get("name", "")
                    org_type = org.get("type", "")
                    org_slug = org.get("slug", "")
                    if org_name:
                        # Include DDP link if slug is available
                        if org_slug:
                            line = f"- [{org_name}](https://digitaldemocracyproject.org/member-organizations/{org_slug})"
                        else:
                            line = f"- {org_name}"
                        if org_type:
                            line += f" ({org_type})"
                        support_lines.append(line)
                parts.append("\n".join(support_lines))

            if opposing_orgs:
                oppose_lines = ["### Organizations Opposing This Bill"]
                for org in opposing_orgs:
                    org_name = org.get("name", "")
                    org_type = org.get("type", "")
                    org_slug = org.get("slug", "")
                    if org_name:
                        # Include DDP link if slug is available
                        if org_slug:
                            line = f"- [{org_name}](https://digitaldemocracyproject.org/member-organizations/{org_slug})"
                        else:
                            line = f"- {org_name}"
                        if org_type:
                            line += f" ({org_type})"
                        oppose_lines.append(line)
                parts.append("\n".join(oppose_lines))

        # External resources section
        external_links = []
        if fields.get("gov-url"):
            external_links.append(f"- [Government Bill Text]({fields['gov-url']})")
        if fields.get("open-plural-url"):
            external_links.append(f"- [Open Plural Discussion]({fields['open-plural-url']})")
        if fields.get("kialo-url"):
            external_links.append(f"- [Kialo Debate]({fields['kialo-url']})")
        if external_links:
            parts.append("## External Resources\n" + "\n".join(external_links))

        return "\n\n".join(parts) if parts else ""

    def _html_to_text(self, html: str) -> str:
        """Convert HTML to plain text."""
        if not html:
            return ""
        soup = BeautifulSoup(html, "html.parser")
        return soup.get_text(separator="\n", strip=True)

    def _get_jurisdiction(self, fields: dict) -> str:
        """Extract jurisdiction from bill fields."""
        # This might be a reference field, so we may need to handle it
        jurisdiction = fields.get("jurisdiction")
        if isinstance(jurisdiction, str):
            return jurisdiction
        if isinstance(jurisdiction, dict):
            return jurisdiction.get("name", "US")
        return "US"

    def _get_bill_id(self, fields: dict) -> str:
        """Construct bill ID from fields."""
        prefix = fields.get("bill-prefix", "")
        number = fields.get("bill-number", "")
        session = fields.get("session-code", fields.get("bill-session", ""))

        if prefix and number:
            bill_id = f"{prefix}-{number}"
            if session:
                bill_id = f"{bill_id}-{session}"
            return bill_id

        return fields.get("govid", fields.get("name", "unknown"))

    async def fetch_item_by_id(
        self,
        collection_id: str,
        item_id: str,
    ) -> dict | None:
        """
        Fetch a single item from Webflow CMS by its ID.

        Args:
            collection_id: Webflow collection ID
            item_id: Webflow item ID

        Returns:
            Item data dict or None if not found
        """
        url = f"{self.BASE_URL}/collections/{collection_id}/items/{item_id}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "accept": "application/json",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(url, headers=headers)
                if response.status_code == 404:
                    logger.warning(
                        "Webflow item not found",
                        collection_id=collection_id,
                        item_id=item_id,
                    )
                    return None
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(
                    "Webflow API error",
                    collection_id=collection_id,
                    item_id=item_id,
                    status_code=e.response.status_code,
                )
                return None
            except Exception as e:
                logger.error(
                    "Failed to fetch Webflow item",
                    collection_id=collection_id,
                    item_id=item_id,
                    error=str(e),
                )
                return None

    async def fetch_item_by_slug(
        self,
        collection_id: str,
        slug: str,
    ) -> dict | None:
        """
        Fetch a single item from Webflow CMS by its slug.

        Note: This requires pagination through the collection, so fetch_item_by_id
        is preferred when the item ID is known.

        Args:
            collection_id: Webflow collection ID
            slug: Item slug to search for

        Returns:
            Item data dict or None if not found
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "accept": "application/json",
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            offset = 0
            page_size = 100

            while True:
                try:
                    params = {"limit": page_size, "offset": offset}
                    response = await client.get(
                        f"{self.BASE_URL}/collections/{collection_id}/items",
                        headers=headers,
                        params=params,
                    )
                    response.raise_for_status()
                    data = response.json()
                    items = data.get("items", [])

                    if not items:
                        break

                    # Search for matching slug
                    for item in items:
                        fields = item.get("fieldData", {})
                        if fields.get("slug") == slug:
                            return item

                    # Check pagination
                    pagination = data.get("pagination", {})
                    total = pagination.get("total", 0)
                    if offset + len(items) >= total or len(items) < page_size:
                        break

                    offset += page_size

                except Exception as e:
                    logger.error(
                        "Failed to search Webflow collection by slug",
                        collection_id=collection_id,
                        slug=slug,
                        error=str(e),
                    )
                    break

            logger.warning(
                "Webflow item not found by slug",
                collection_id=collection_id,
                slug=slug,
            )
            return None

    async def fetch_page(self, url: str) -> DocumentSource | None:
        """
        Fetch a specific Webflow page by URL.

        Args:
            url: URL of the page

        Returns:
            DocumentSource or None if not found
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
                html = response.text
            except Exception as e:
                logger.error(
                    "Failed to fetch Webflow page",
                    url=url,
                    error=str(e),
                )
                return None

            # Parse HTML
            soup = BeautifulSoup(html, "html.parser")

            # Extract title
            title = None
            title_tag = soup.find("title")
            if title_tag:
                title = title_tag.get_text(strip=True)

            # Extract main content
            content = self._extract_html_content(soup)
            if not content:
                return None

            metadata = self.metadata_extractor.extract_web_content_metadata(
                url=url,
                title=title,
                content_type="webpage",
            )
            metadata.source = "webflow"

            return DocumentSource(
                content=content,
                metadata=metadata,
            )

    def _extract_html_content(self, soup: BeautifulSoup) -> str:
        """Extract text content from parsed HTML."""
        # Remove unwanted elements
        for element in soup(["script", "style", "nav", "footer", "header", "aside"]):
            element.decompose()

        # Try to find main content
        main = soup.find("main") or soup.find("article") or soup.find("div", class_="content")

        if main:
            text = main.get_text(separator="\n\n", strip=True)
        else:
            text = soup.get_text(separator="\n\n", strip=True)

        # Clean up whitespace
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        return "\n\n".join(lines)
