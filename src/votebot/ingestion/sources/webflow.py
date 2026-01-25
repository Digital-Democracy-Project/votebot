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
        self.pdf_source = PDFSource(self.settings, self.metadata_extractor)

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

        # Extract CMS content
        cms_content = self._extract_bill_content(fields)

        # Create base metadata
        metadata = DocumentMetadata(
            document_id=f"bill-webflow-{item_id}",
            document_type="bill",
            source="webflow-cms",
            title=name,
            jurisdiction=self._get_jurisdiction(fields),
            bill_id=self._get_bill_id(fields),
            url=fields.get("gov-url"),
            extra={
                "webflow_id": item_id,
                "description": fields.get("description", "")[:500],
                "status": fields.get("status"),
                "session": fields.get("bill-session"),
                "bill_prefix": fields.get("bill-prefix"),
                "bill_number": fields.get("bill-number"),
            },
        )

        # Yield CMS content first
        if cms_content:
            yield DocumentSource(
                content=cms_content,
                metadata=metadata,
            )

        # Process PDF if gov-url is available
        gov_url = fields.get("gov-url")
        if include_pdfs and gov_url and gov_url.endswith(".pdf"):
            pdf_doc = await self._process_bill_pdf(gov_url, fields, item_id)
            if pdf_doc:
                yield pdf_doc

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
            metadata = DocumentMetadata(
                document_id=f"bill-pdf-{item_id}",
                document_type="bill-text",
                source="webflow-pdf",
                title=f"{name} - Full Text",
                jurisdiction=self._get_jurisdiction(fields),
                bill_id=self._get_bill_id(fields),
                url=pdf_url,
                extra={
                    "webflow_id": item_id,
                    "content_type": "pdf",
                    "session": fields.get("bill-session"),
                    "bill_prefix": fields.get("bill-prefix"),
                    "bill_number": fields.get("bill-number"),
                },
            )

            return DocumentSource(
                content=doc.content,
                metadata=metadata,
            )

        except Exception as e:
            logger.warning(
                "Failed to process bill PDF",
                url=pdf_url,
                error=str(e),
            )
            return None

    def _extract_bill_content(self, fields: dict) -> str:
        """Extract text content from bill CMS fields."""
        parts = []

        # Name/Title
        if fields.get("name"):
            parts.append(f"# {fields['name']}")

        # Description
        if fields.get("description"):
            parts.append(f"## Description\n{fields['description']}")

        # Status and session info
        info_parts = []
        if fields.get("status"):
            info_parts.append(f"**Status:** {fields['status']}")
        if fields.get("bill-session"):
            info_parts.append(f"**Session:** {fields['bill-session']}")
        if fields.get("bill-prefix") and fields.get("bill-number"):
            info_parts.append(f"**Bill Number:** {fields['bill-prefix']} {fields['bill-number']}")
        if info_parts:
            parts.append("\n".join(info_parts))

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

        # Post body (main content)
        if fields.get("post-body"):
            body_text = self._html_to_text(fields["post-body"])
            if body_text:
                parts.append(f"## Details\n{body_text}")

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
