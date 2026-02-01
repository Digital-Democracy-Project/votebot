"""Organization sync service for ingesting organization content to Pinecone."""

from dataclasses import dataclass
from datetime import date

import httpx
import structlog
from bs4 import BeautifulSoup

from votebot.config import Settings, get_settings
from votebot.ingestion.metadata import DocumentMetadata
from votebot.ingestion.pipeline import IngestionPipeline

logger = structlog.get_logger()


@dataclass
class OrganizationSyncResult:
    """Result of syncing a single organization."""

    organization_id: str
    organization_name: str
    success: bool
    chunks_created: int = 0
    error: str | None = None


class OrganizationSyncService:
    """
    Service for syncing organization content to Pinecone.

    Processes organization data from Webflow CMS and ingests it
    into the vector store for RAG retrieval.
    """

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the organization sync service.

        Args:
            settings: Application settings
        """
        self.settings = settings or get_settings()
        self.pipeline = IngestionPipeline(self.settings)
        # Cache for bill lookups (bill_id -> {name, slug, identifier})
        self._bill_cache: dict[str, dict] = {}

    async def _build_bill_mapping(self) -> None:
        """
        Build a mapping from bill IDs to bill info for resolving references.

        Fetches the bills collection from Webflow and creates a lookup table.
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

        logger.info("Building bill mapping for organization sync...")

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
                        item_id = item.get("id", "")
                        fields = item.get("fieldData", {})
                        name = fields.get("name", "")
                        slug = fields.get("slug", "")
                        bill_prefix = fields.get("bill-prefix", "")
                        bill_number = fields.get("bill-number", "")
                        identifier = f"{bill_prefix} {bill_number}".strip()

                        if item_id:
                            self._bill_cache[item_id] = {
                                "name": name,
                                "slug": slug,
                                "identifier": identifier if identifier else name,
                            }

                    pagination = data.get("pagination", {})
                    total = pagination.get("total", 0)
                    if offset + len(items) >= total or len(items) < page_size:
                        break

                    offset += page_size

            logger.info(f"Built bill mapping with {len(self._bill_cache)} entries")

        except Exception as e:
            logger.warning("Failed to build bill mapping", error=str(e))

    def _resolve_bill_references(self, bill_refs: list | None) -> list[dict]:
        """
        Resolve bill reference IDs to bill information.

        Args:
            bill_refs: List of bill reference IDs from Webflow

        Returns:
            List of dicts with bill name, identifier, and slug
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
                    resolved.append({
                        "name": f"Bill {ref[:8]}...",
                        "identifier": ref,
                        "slug": "",
                    })
            elif isinstance(ref, dict):
                resolved.append(ref)

        return resolved

    def _html_to_text(self, html: str) -> str:
        """Convert HTML to plain text."""
        if not html:
            return ""
        soup = BeautifulSoup(html, "html.parser")
        return soup.get_text(separator="\n", strip=True)

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
            if desc_text and desc_text != about:
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

    async def sync_organization(
        self,
        webflow_item: dict,
    ) -> OrganizationSyncResult:
        """
        Sync a single organization to Pinecone.

        Args:
            webflow_item: Organization item data from Webflow CMS

        Returns:
            OrganizationSyncResult with sync status
        """
        item_id = webflow_item.get("id", "")
        fields = webflow_item.get("fieldData", {})
        name = fields.get("name", "Unknown Organization")

        if not name or name == "Unknown Organization":
            return OrganizationSyncResult(
                organization_id=item_id,
                organization_name=name,
                success=False,
                error="Organization has no name",
            )

        # Build bill mapping if not cached
        await self._build_bill_mapping()

        # Resolve bill references
        bills_support = self._resolve_bill_references(fields.get("bills-support", []))
        bills_oppose = self._resolve_bill_references(fields.get("bills-oppose", []))

        # Extract content
        content = self._extract_organization_content(fields, bills_support, bills_oppose)

        if not content:
            return OrganizationSyncResult(
                organization_id=item_id,
                organization_name=name,
                success=False,
                error="No content extracted",
            )

        # Create metadata with DDP URL for citation linking
        slug = fields.get("slug", "")
        ddp_url = f"https://digitaldemocracyproject.org/member-organizations/{slug}" if slug else None

        metadata = DocumentMetadata(
            document_id=f"organization-{item_id}",
            document_type="organization",
            source="Digital Democracy Project",
            title=name,
            jurisdiction=None,
            url=ddp_url,
            extra={
                "webflow_id": item_id,
                "slug": slug,
                "organization_type": fields.get("type-2", ""),
                "website": fields.get("website", ""),
                "bills_support_count": len(bills_support),
                "bills_oppose_count": len(bills_oppose),
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

            return OrganizationSyncResult(
                organization_id=item_id,
                organization_name=name,
                success=True,
                chunks_created=result.chunks_created,
            )

        except Exception as e:
            logger.error(
                "Failed to ingest organization",
                organization_id=item_id,
                error=str(e),
            )
            return OrganizationSyncResult(
                organization_id=item_id,
                organization_name=name,
                success=False,
                error=str(e),
            )
