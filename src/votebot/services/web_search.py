"""Web search service using Tavily API for RAG fallback."""

from dataclasses import dataclass

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from votebot.config import Settings, get_settings

logger = structlog.get_logger()

TAVILY_API_URL = "https://api.tavily.com/search"


@dataclass
class WebSearchResult:
    """A web search result."""

    title: str
    url: str
    snippet: str
    source: str
    score: float = 0.0


class WebSearchService:
    """
    Service for web search fallback when RAG doesn't have sufficient information.

    Uses Tavily API for reliable, AI-optimized web search results.
    """

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the web search service.

        Args:
            settings: Application settings. Uses default if not provided.
        """
        self.settings = settings or get_settings()
        self.client = httpx.AsyncClient(timeout=30.0)
        self._api_key = self.settings.tavily_api_key.get_secret_value()

    def is_configured(self) -> bool:
        """Check if web search is properly configured."""
        return bool(self._api_key and self.settings.web_search_enabled)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    async def search(
        self,
        query: str,
        num_results: int = 5,
        search_depth: str = "basic",
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
    ) -> list[WebSearchResult]:
        """
        Perform a web search using Tavily API.

        Args:
            query: The search query
            num_results: Maximum number of results (default 5)
            search_depth: "basic" or "advanced" (advanced is slower but more thorough)
            include_domains: Only include results from these domains
            exclude_domains: Exclude results from these domains

        Returns:
            List of WebSearchResult objects
        """
        if not self.is_configured():
            logger.warning("Web search not configured - missing TAVILY_API_KEY")
            return []

        logger.info(
            "Performing web search",
            query=query,
            num_results=num_results,
            search_depth=search_depth,
        )

        payload = {
            "api_key": self._api_key,
            "query": query,
            "max_results": num_results,
            "search_depth": search_depth,
            "include_answer": False,  # We generate our own answer
            "include_raw_content": False,  # Just snippets for context
        }

        if include_domains:
            payload["include_domains"] = include_domains
        if exclude_domains:
            payload["exclude_domains"] = exclude_domains

        try:
            response = await self.client.post(TAVILY_API_URL, json=payload)
            response.raise_for_status()
            data = response.json()

            results = []
            for item in data.get("results", []):
                results.append(
                    WebSearchResult(
                        title=item.get("title", ""),
                        url=item.get("url", ""),
                        snippet=item.get("content", ""),
                        source="web",
                        score=item.get("score", 0.0),
                    )
                )

            logger.info(
                "Web search completed",
                query=query,
                results_count=len(results),
            )
            return results

        except httpx.HTTPStatusError as e:
            logger.error(
                "Web search HTTP error",
                status_code=e.response.status_code,
                error=str(e),
            )
            return []
        except Exception as e:
            logger.error("Web search failed", error=str(e))
            return []

    async def search_legislation(
        self,
        query: str,
        num_results: int = 5,
    ) -> list[WebSearchResult]:
        """
        Search for legislation-related information.

        Focuses on authoritative government and news sources.

        Args:
            query: The search query
            num_results: Maximum number of results

        Returns:
            List of WebSearchResult objects
        """
        # Prioritize authoritative sources for legislation
        include_domains = [
            "congress.gov",
            "govtrack.us",
            "legiscan.com",
            "ballotpedia.org",
            "ncsl.org",  # National Conference of State Legislatures
        ]

        return await self.search(
            query=query,
            num_results=num_results,
            search_depth="advanced",
            include_domains=include_domains,
        )

    async def search_legislator(
        self,
        query: str,
        num_results: int = 5,
    ) -> list[WebSearchResult]:
        """
        Search for legislator-related information.

        Focuses on official government and news sources.

        Args:
            query: The search query
            num_results: Maximum number of results

        Returns:
            List of WebSearchResult objects
        """
        # Include news sources for recent legislator information
        include_domains = [
            "congress.gov",
            "ballotpedia.org",
            "votesmart.org",
            "opensecrets.org",
        ]

        return await self.search(
            query=query,
            num_results=num_results,
            search_depth="advanced",
            include_domains=include_domains,
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
        # Add news-related terms and exclude some low-quality sources
        news_query = f"{query} news recent"
        exclude_domains = [
            "pinterest.com",
            "facebook.com",
            "twitter.com",
        ]

        return await self.search(
            query=news_query,
            num_results=num_results,
            exclude_domains=exclude_domains,
        )

    def format_results_for_context(
        self,
        results: list[WebSearchResult],
        max_length: int = 2000,
    ) -> str:
        """
        Format search results as context for the LLM.

        Args:
            results: List of web search results
            max_length: Maximum total length of formatted context

        Returns:
            Formatted string with search results
        """
        if not results:
            return ""

        formatted_parts = ["Web Search Results:"]
        current_length = len(formatted_parts[0])

        for i, result in enumerate(results, 1):
            entry = f"\n[{i}] {result.title}\n    URL: {result.url}\n    {result.snippet}"
            if current_length + len(entry) > max_length:
                break
            formatted_parts.append(entry)
            current_length += len(entry)

        return "\n".join(formatted_parts)

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
