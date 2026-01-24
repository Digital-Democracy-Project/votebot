"""Webflow CMS data source connector."""

from typing import AsyncIterator

import httpx
import structlog
from bs4 import BeautifulSoup

from votebot.config import Settings, get_settings
from votebot.ingestion.metadata import MetadataExtractor
from votebot.ingestion.pipeline import DocumentSource

logger = structlog.get_logger()


class WebflowSource:
    """
    Data source connector for Webflow CMS.

    Fetches DDP content including:
    - Educational materials
    - Bill pages
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
        # Webflow API key would be in settings
        self.api_key = ""  # TODO: Add to settings

    async def fetch(
        self,
        site_id: str,
        collection_id: str | None = None,
        limit: int = 100,
        **kwargs,
    ) -> AsyncIterator[DocumentSource]:
        """
        Fetch content from Webflow CMS.

        Args:
            site_id: Webflow site ID
            collection_id: Optional collection ID to filter
            limit: Maximum number of items to fetch

        Yields:
            DocumentSource objects for each content item
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "accept": "application/json",
            }

            # First get collections if no specific collection
            if collection_id:
                collections = [{"_id": collection_id}]
            else:
                try:
                    response = await client.get(
                        f"{self.BASE_URL}/sites/{site_id}/collections",
                        headers=headers,
                    )
                    response.raise_for_status()
                    collections = response.json().get("collections", [])
                except Exception as e:
                    logger.error("Failed to fetch Webflow collections", error=str(e))
                    return

            # Fetch items from each collection
            for collection in collections:
                coll_id = collection.get("_id")
                if not coll_id:
                    continue

                logger.info(
                    "Fetching from Webflow collection",
                    collection_id=coll_id,
                )

                try:
                    response = await client.get(
                        f"{self.BASE_URL}/collections/{coll_id}/items",
                        headers=headers,
                        params={"limit": min(limit, 100)},
                    )
                    response.raise_for_status()
                    items = response.json().get("items", [])
                except Exception as e:
                    logger.warning(
                        "Failed to fetch collection items",
                        collection_id=coll_id,
                        error=str(e),
                    )
                    continue

                for item in items:
                    try:
                        content = self._extract_item_content(item)
                        if not content:
                            continue

                        metadata = self.metadata_extractor.extract_web_content_metadata(
                            url=item.get("_cmsUrl", ""),
                            title=item.get("name"),
                            content_type="cms",
                        )

                        # Add Webflow-specific metadata
                        metadata.extra["webflow_id"] = item.get("_id")
                        metadata.extra["collection_id"] = coll_id
                        metadata.source = "webflow"

                        yield DocumentSource(
                            content=content,
                            metadata=metadata,
                        )

                    except Exception as e:
                        logger.warning(
                            "Failed to process Webflow item",
                            item_id=item.get("_id"),
                            error=str(e),
                        )
                        continue

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

    async def fetch_sitemap(
        self,
        site_url: str,
        limit: int = 100,
    ) -> AsyncIterator[DocumentSource]:
        """
        Fetch all pages from a Webflow site using its sitemap.

        Args:
            site_url: Base URL of the site
            limit: Maximum number of pages to fetch

        Yields:
            DocumentSource objects for each page
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            sitemap_url = f"{site_url.rstrip('/')}/sitemap.xml"

            try:
                response = await client.get(sitemap_url)
                response.raise_for_status()
                sitemap_xml = response.text
            except Exception as e:
                logger.error(
                    "Failed to fetch sitemap",
                    url=sitemap_url,
                    error=str(e),
                )
                return

            # Parse sitemap
            soup = BeautifulSoup(sitemap_xml, "xml")
            urls = [loc.text for loc in soup.find_all("loc")][:limit]

            logger.info(f"Found {len(urls)} URLs in sitemap")

            for url in urls:
                doc = await self.fetch_page(url)
                if doc:
                    yield doc

    def _extract_item_content(self, item: dict) -> str:
        """Extract text content from a Webflow CMS item."""
        parts = []

        # Name/Title
        if item.get("name"):
            parts.append(f"# {item['name']}")

        # Iterate through all fields
        for key, value in item.items():
            if key.startswith("_"):
                continue
            if isinstance(value, str) and len(value) > 10:
                # Check if it's HTML
                if "<" in value and ">" in value:
                    soup = BeautifulSoup(value, "html.parser")
                    text = soup.get_text(separator="\n", strip=True)
                    if text:
                        parts.append(f"## {key.replace('-', ' ').title()}")
                        parts.append(text)
                else:
                    parts.append(f"**{key.replace('-', ' ').title()}:** {value}")

        return "\n\n".join(parts) if parts else ""

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
