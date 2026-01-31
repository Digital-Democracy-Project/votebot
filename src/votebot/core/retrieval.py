"""RAG retrieval orchestration service."""

from dataclasses import dataclass, field

import structlog

from votebot.api.schemas.chat import PageContext
from votebot.config import Settings, get_settings
from votebot.services.vector_store import SearchResult, VectorStoreService

logger = structlog.get_logger()


@dataclass
class RetrievalResult:
    """Result of a retrieval operation."""

    chunks: list[SearchResult]
    query_used: str
    filters_applied: dict
    total_retrieved: int


@dataclass
class RetrievalConfig:
    """Configuration for retrieval operations."""

    max_chunks: int = 10
    similarity_threshold: float = 0.7
    use_hybrid_search: bool = True
    deduplicate: bool = True


class RetrievalService:
    """
    Service for RAG retrieval orchestration.

    Handles:
    - Semantic search with metadata filters
    - Hybrid retrieval (semantic + keyword)
    - Chunk deduplication
    - Context-aware filtering
    """

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the retrieval service.

        Args:
            settings: Application settings. Uses default if not provided.
        """
        self.settings = settings or get_settings()
        self.vector_store = VectorStoreService(self.settings)
        self.config = RetrievalConfig(
            max_chunks=self.settings.max_retrieval_chunks,
            similarity_threshold=self.settings.similarity_threshold,
        )

    async def retrieve(
        self,
        query: str,
        page_context: PageContext,
        max_chunks: int | None = None,
    ) -> RetrievalResult:
        """
        Retrieve relevant chunks for a query.

        For bill queries, prioritizes actual legislative text (document_type="bill-text")
        over CMS summaries (document_type="bill").

        Args:
            query: The user's query
            page_context: Context about the current page
            max_chunks: Override default max chunks

        Returns:
            RetrievalResult with retrieved chunks
        """
        max_chunks = max_chunks or self.config.max_chunks

        # Build filters based on page context
        filters = self._build_filters(page_context)

        logger.info(
            "Starting retrieval",
            query_length=len(query),
            page_type=page_context.type,
            filters=filters,
        )

        # For bill queries, use two-phase retrieval to prioritize legislative text
        if page_context.type == "bill":
            final_results = await self._retrieve_bill_with_text_priority(
                query=query,
                filters=filters,
                max_chunks=max_chunks,
            )
        else:
            # Standard retrieval for non-bill queries
            results = await self.vector_store.query(
                query=query,
                top_k=max_chunks * 2,
                filter=filters if filters else None,
            )

            # Filter by similarity threshold
            filtered_results = [
                r for r in results if r.score >= self.config.similarity_threshold
            ]

            # Deduplicate if enabled
            if self.config.deduplicate:
                filtered_results = self._deduplicate(filtered_results)

            final_results = filtered_results[:max_chunks]

        logger.info(
            "Retrieval completed",
            final_count=len(final_results),
            page_type=page_context.type,
        )

        return RetrievalResult(
            chunks=final_results,
            query_used=query,
            filters_applied=filters,
            total_retrieved=len(final_results),
        )

    async def _retrieve_bill_with_text_priority(
        self,
        query: str,
        filters: dict,
        max_chunks: int,
    ) -> list[SearchResult]:
        """
        Retrieve bill content with priority for actual legislative text.

        First tries to get chunks from bill-text (PDF/legislative text),
        then fills remaining slots with bill summary content.

        Args:
            query: The search query
            filters: Base filters (bill_id, jurisdiction)
            max_chunks: Maximum chunks to return

        Returns:
            List of SearchResult prioritizing legislative text
        """
        # Phase 1: Get legislative text chunks (document_type="bill-text")
        text_filters = {**filters, "document_type": "bill-text"}
        text_results = await self.vector_store.query(
            query=query,
            top_k=max_chunks,
            filter=text_filters,
        )
        text_results = [
            r for r in text_results if r.score >= self.config.similarity_threshold
        ]

        logger.info(
            "Bill text retrieval phase 1",
            text_chunks_found=len(text_results),
        )

        # Phase 2: If we don't have enough, get summary content
        remaining_slots = max_chunks - len(text_results)
        summary_results = []

        if remaining_slots > 0:
            # Get bill summaries (document_type="bill")
            summary_filters = {**filters, "document_type": "bill"}
            summary_results = await self.vector_store.query(
                query=query,
                top_k=remaining_slots * 2,
                filter=summary_filters,
            )
            summary_results = [
                r for r in summary_results if r.score >= self.config.similarity_threshold
            ]

            logger.info(
                "Bill text retrieval phase 2",
                summary_chunks_found=len(summary_results),
            )

        # Combine results: legislative text first, then summaries
        combined = text_results + summary_results

        # Deduplicate
        if self.config.deduplicate:
            combined = self._deduplicate(combined)

        # If we still don't have results, try without document_type filter
        if not combined:
            logger.info("No typed results, falling back to unfiltered query")
            all_results = await self.vector_store.query(
                query=query,
                top_k=max_chunks * 2,
                filter=filters if filters else None,
            )
            combined = [
                r for r in all_results if r.score >= self.config.similarity_threshold
            ]
            if self.config.deduplicate:
                combined = self._deduplicate(combined)

        return combined[:max_chunks]

    async def retrieve_for_bill(
        self,
        query: str,
        bill_id: str,
        jurisdiction: str | None = None,
    ) -> RetrievalResult:
        """
        Retrieve chunks specifically about a bill.

        Args:
            query: The user's query
            bill_id: The bill identifier
            jurisdiction: Optional jurisdiction filter

        Returns:
            RetrievalResult with bill-specific chunks
        """
        page_context = PageContext(
            type="bill",
            id=bill_id,
            jurisdiction=jurisdiction,
        )
        return await self.retrieve(query, page_context)

    async def retrieve_for_legislator(
        self,
        query: str,
        legislator_id: str,
        jurisdiction: str | None = None,
    ) -> RetrievalResult:
        """
        Retrieve chunks specifically about a legislator.

        Args:
            query: The user's query
            legislator_id: The legislator identifier
            jurisdiction: Optional jurisdiction filter

        Returns:
            RetrievalResult with legislator-specific chunks
        """
        page_context = PageContext(
            type="legislator",
            id=legislator_id,
            jurisdiction=jurisdiction,
        )
        return await self.retrieve(query, page_context)

    async def retrieve_general(
        self,
        query: str,
        jurisdiction: str | None = None,
    ) -> RetrievalResult:
        """
        Retrieve chunks for general queries.

        Args:
            query: The user's query
            jurisdiction: Optional jurisdiction filter

        Returns:
            RetrievalResult with relevant chunks
        """
        page_context = PageContext(
            type="general",
            jurisdiction=jurisdiction,
        )
        return await self.retrieve(query, page_context)

    def _build_filters(self, page_context: PageContext) -> dict:
        """
        Build Pinecone filters from page context.

        Args:
            page_context: The current page context

        Returns:
            Filter dictionary for Pinecone query
        """
        filters = {}

        # Add hard filters for bill/legislator pages
        if page_context.type == "bill" and page_context.id:
            filters["bill_id"] = page_context.id
        elif page_context.type == "legislator" and page_context.id:
            filters["legislator_id"] = page_context.id

        # Add jurisdiction filter if specified
        if page_context.jurisdiction:
            filters["jurisdiction"] = page_context.jurisdiction

        return filters

    def _deduplicate(self, results: list[SearchResult]) -> list[SearchResult]:
        """
        Remove duplicate or near-duplicate chunks.

        Uses content hashing to identify duplicates.

        Args:
            results: List of search results

        Returns:
            Deduplicated list of search results
        """
        seen_content = set()
        deduplicated = []

        for result in results:
            # Create a simple hash of the content
            content_hash = hash(result.content[:500])

            if content_hash not in seen_content:
                seen_content.add(content_hash)
                deduplicated.append(result)

        if len(deduplicated) < len(results):
            logger.debug(
                "Deduplicated results",
                original=len(results),
                deduplicated=len(deduplicated),
            )

        return deduplicated


class HybridRetrievalService(RetrievalService):
    """
    Extended retrieval service with hybrid search support.

    Combines semantic search with keyword matching for improved results.
    """

    async def retrieve(
        self,
        query: str,
        page_context: PageContext,
        max_chunks: int | None = None,
    ) -> RetrievalResult:
        """
        Retrieve using hybrid search (semantic + keyword).

        Args:
            query: The user's query
            page_context: Context about the current page
            max_chunks: Override default max chunks

        Returns:
            RetrievalResult with retrieved chunks
        """
        max_chunks = max_chunks or self.config.max_chunks

        # Get semantic results
        semantic_result = await super().retrieve(
            query=query,
            page_context=page_context,
            max_chunks=max_chunks,
        )

        # For hybrid search, we would also do keyword search
        # and merge results. For now, return semantic results.
        # TODO: Implement keyword search using Pinecone sparse vectors
        # or a separate keyword index

        return semantic_result

    async def _keyword_search(
        self,
        query: str,
        filters: dict,
        top_k: int,
    ) -> list[SearchResult]:
        """
        Perform keyword-based search.

        This is a placeholder for future implementation using
        Pinecone sparse vectors or a separate search index.
        """
        # TODO: Implement keyword search
        return []

    def _merge_results(
        self,
        semantic: list[SearchResult],
        keyword: list[SearchResult],
        max_results: int,
    ) -> list[SearchResult]:
        """
        Merge semantic and keyword results using reciprocal rank fusion.

        Args:
            semantic: Semantic search results
            keyword: Keyword search results
            max_results: Maximum results to return

        Returns:
            Merged and ranked results
        """
        # Reciprocal Rank Fusion (RRF)
        k = 60  # RRF constant
        scores = {}

        # Score semantic results
        for rank, result in enumerate(semantic):
            scores[result.id] = scores.get(result.id, 0) + 1 / (k + rank + 1)

        # Score keyword results
        for rank, result in enumerate(keyword):
            scores[result.id] = scores.get(result.id, 0) + 1 / (k + rank + 1)

        # Create result lookup
        all_results = {r.id: r for r in semantic + keyword}

        # Sort by RRF score
        sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)

        return [all_results[id] for id in sorted_ids[:max_results]]
