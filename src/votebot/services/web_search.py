"""Optional web search fallback service."""

from dataclasses import dataclass

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from votebot.config import Settings, get_settings

logger = structlog.get_logger()


@dataclass
class WebSearchResult:
    """A web search result."""

    title: str
    url: str
    snippet: str
    source: str


class WebSearchService:
    """
    Service for web search fallback when RAG doesn't have sufficient information.

    This is an optional service that can be used to supplement RAG results
    with real-time web search for current events or recent changes.
    """

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the web search service.

        Args:
            settings: Application settings. Uses default if not provided.
        """
        self.settings = settings or get_settings()
        self.client = httpx.AsyncClient(timeout=30.0)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    async def search(
        self,
        query: str,
        num_results: int = 5,
        site_filter: str | None = None,
    ) -> list[WebSearchResult]:
        """
        Perform a web search.

        Args:
            query: The search query
            num_results: Maximum number of results
            site_filter: Optional site to restrict search to

        Returns:
            List of WebSearchResult objects

        Note:
            This is a placeholder implementation. In production, you would
            integrate with a search API like Google Custom Search, Bing,
            or SerpAPI.
        """
        logger.info(
            "Web search requested",
            query=query,
            num_results=num_results,
        )

        # Placeholder - in production, integrate with a search API
        # Example using SerpAPI:
        # response = await self.client.get(
        #     "https://serpapi.com/search",
        #     params={
        #         "q": query,
        #         "api_key": self.settings.serpapi_key,
        #         "num": num_results,
        #     }
        # )
        # data = response.json()
        # return [WebSearchResult(...) for result in data["organic_results"]]

        logger.warning("Web search not implemented - returning empty results")
        return []

    async def search_congress(
        self,
        query: str,
        num_results: int = 5,
    ) -> list[WebSearchResult]:
        """
        Search Congress.gov specifically.

        Args:
            query: The search query
            num_results: Maximum number of results

        Returns:
            List of WebSearchResult objects
        """
        return await self.search(
            query=query,
            num_results=num_results,
            site_filter="congress.gov",
        )

    async def search_news(
        self,
        query: str,
        num_results: int = 5,
    ) -> list[WebSearchResult]:
        """
        Search for recent news about a topic.

        Args:
            query: The search query
            num_results: Maximum number of results

        Returns:
            List of WebSearchResult objects
        """
        # Add news-related terms to the query
        news_query = f"{query} news"
        return await self.search(
            query=news_query,
            num_results=num_results,
        )

    async def close(self) -> None:
        """Close the HTTP client."""
        await self.client.aclose()


class WebSearchServiceFactory:
    """Factory for creating web search service instances."""

    _instance: WebSearchService | None = None

    @classmethod
    def get_instance(cls, settings: Settings | None = None) -> WebSearchService:
        """Get or create a singleton web search service instance."""
        if cls._instance is None:
            cls._instance = WebSearchService(settings)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton instance (useful for testing)."""
        cls._instance = None
