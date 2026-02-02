"""RAG retrieval orchestration service."""

import re
from dataclasses import dataclass, field

import structlog

from votebot.api.schemas.chat import PageContext
from votebot.config import Settings, get_settings
from votebot.services.vector_store import SearchResult, VectorStoreService

logger = structlog.get_logger()


# Mapping of state names/abbreviations to jurisdiction codes
STATE_MAPPINGS = {
    "florida": "fl", "fl": "fl",
    "virginia": "va", "va": "va",
    "washington": "wa", "wa": "wa",
    "california": "ca", "ca": "ca",
    "texas": "tx", "tx": "tx",
    "new york": "ny", "ny": "ny",
    "arizona": "az", "az": "az",
    "michigan": "mi", "mi": "mi",
    "utah": "ut", "ut": "ut",
    "alabama": "al", "al": "al",
    "massachusetts": "ma", "ma": "ma",
    "federal": "us", "us": "us", "congress": "us",
}


@dataclass
class ExtractedBillInfo:
    """Bill information extracted from query text."""
    bill_prefix: str  # HB, SB, HR, S, etc.
    bill_number: str  # 363, 429, etc.
    jurisdiction: str | None = None  # fl, va, us, etc.

    @property
    def bill_id(self) -> str:
        """Return normalized bill ID like HB363."""
        return f"{self.bill_prefix}{self.bill_number}"

    @property
    def slug_pattern(self) -> str:
        """Return pattern to match in slug like hb363 or hb-363."""
        prefix = self.bill_prefix.lower()
        num = self.bill_number
        # Match patterns like: hb363, hb-363, hb 363
        return f"{prefix}[-]?{num}"


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

        For general queries, attempts to extract bill identifiers from the query text
        and use them for filtering.

        Args:
            query: The user's query
            page_context: Context about the current page
            max_chunks: Override default max chunks

        Returns:
            RetrievalResult with retrieved chunks
        """
        max_chunks = max_chunks or self.config.max_chunks

        # For general queries, try to extract bill info from query and upgrade context
        effective_context = page_context
        if page_context.type == "general":
            bill_info = self._extract_bill_from_query(query)
            if bill_info:
                logger.info(
                    "Extracted bill from query",
                    bill_id=bill_info.bill_id,
                    jurisdiction=bill_info.jurisdiction,
                )
                # Look up the actual slug for this bill
                slug = await self._lookup_bill_slug(bill_info)
                if slug:
                    # Upgrade to bill context with the found slug
                    effective_context = PageContext(
                        type="bill",
                        slug=slug,
                        title=f"{bill_info.bill_prefix} {bill_info.bill_number}",
                        jurisdiction=bill_info.jurisdiction.upper() if bill_info.jurisdiction else None,
                    )
                    logger.info(
                        "Upgraded to bill context from query extraction",
                        slug=slug,
                        original_context="general",
                    )

        # Build filters based on effective context
        filters = self._build_filters(effective_context, query)

        logger.info(
            "Starting retrieval",
            query_length=len(query),
            page_type=effective_context.type,
            filters=filters,
        )

        # For bill queries, use multi-phase retrieval to prioritize legislative text
        if effective_context.type == "bill":
            final_results = await self._retrieve_bill_with_text_priority(
                query=query,
                filters=filters,
                max_chunks=max_chunks,
                page_context=effective_context,
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
            page_type=effective_context.type,
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
        page_context: PageContext | None = None,
    ) -> list[SearchResult]:
        """
        Retrieve bill content with priority for actual legislative text.

        Phase 1: Get bill-text (PDF/legislative text) with webflow_id filter
        Phase 2: Get bill summaries with webflow_id filter
        Phase 3: Get bill-history (no webflow_id in metadata) using semantic search

        Args:
            query: The search query
            filters: Base filters (webflow_id)
            max_chunks: Maximum chunks to return
            page_context: Page context with bill info for enhanced history search

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

        # Phase 3: Get legislative history
        # Filter by webflow_id if available to ensure we get the right bill's history
        remaining_slots = max_chunks - len(text_results) - len(summary_results)
        history_results = []

        if remaining_slots > 0:
            # Build enhanced query with bill identifiers for better matching
            history_query = query
            if page_context:
                bill_id = page_context.id or ""
                bill_title = page_context.title or ""
                if bill_id or bill_title:
                    history_query = f"{bill_id} {bill_title} {query}".strip()

            history_filters = {"document_type": "bill-history"}
            # Apply webflow_id filter if available to get the correct bill's history
            if filters.get("webflow_id"):
                history_filters["webflow_id"] = filters["webflow_id"]
            elif filters.get("slug"):
                history_filters["slug"] = filters["slug"]
            history_results = await self.vector_store.query(
                query=history_query,
                top_k=remaining_slots * 2,
                filter=history_filters,
            )
            history_results = [
                r for r in history_results if r.score >= self.config.similarity_threshold
            ]

            logger.info(
                "Bill text retrieval phase 3",
                history_chunks_found=len(history_results),
                history_query_preview=history_query[:50],
            )

        # Combine results: legislative text first, then summaries, then history
        combined = text_results + summary_results + history_results

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

    def _extract_bill_from_query(self, query: str) -> ExtractedBillInfo | None:
        """
        Extract bill identifier from query text.

        Handles patterns like:
        - "HB 363", "HB363", "H.B. 363"
        - "Florida HB 363", "FL HB 363"
        - "HR 1004", "H.R. 1004"
        - "Senate Bill 123", "SB 123"

        Args:
            query: The user's query text

        Returns:
            ExtractedBillInfo if a bill identifier is found, None otherwise
        """
        query_lower = query.lower()

        # Extract jurisdiction from query
        jurisdiction = None
        for name, code in STATE_MAPPINGS.items():
            if name in query_lower:
                jurisdiction = code
                break

        # Patterns to match bill identifiers
        # Pattern 1: Standard bill format (HB 363, SB 123, HR 1004, S 302)
        pattern1 = r'\b(H\.?B\.?|S\.?B\.?|H\.?R\.?|S\.?|H\.?J\.?|S\.?J\.?)\s*(\d+)\b'

        # Pattern 2: Full names (House Bill 363, Senate Bill 123)
        pattern2 = r'\b(house|senate)\s+(?:bill|resolution|joint\s+resolution)\s*(\d+)\b'

        match = re.search(pattern1, query, re.IGNORECASE)
        if match:
            prefix = match.group(1).replace(".", "").upper()
            number = match.group(2)
            return ExtractedBillInfo(
                bill_prefix=prefix,
                bill_number=number,
                jurisdiction=jurisdiction,
            )

        match = re.search(pattern2, query, re.IGNORECASE)
        if match:
            chamber = match.group(1).lower()
            number = match.group(2)
            prefix = "HB" if chamber == "house" else "SB"
            return ExtractedBillInfo(
                bill_prefix=prefix,
                bill_number=number,
                jurisdiction=jurisdiction,
            )

        return None

    async def _lookup_bill_slug(self, bill_info: ExtractedBillInfo) -> str | None:
        """
        Look up the actual slug for a bill in Pinecone.

        Uses a two-phase approach:
        1. Try filtering by bill_id metadata with common year patterns
        2. Fall back to semantic search with slug pattern matching

        Args:
            bill_info: Extracted bill information

        Returns:
            The bill's slug if found, None otherwise
        """
        # Phase 1: Try direct bill_id filter with common years
        # Bill IDs are stored as "HB-363-2026" format
        current_year = 2026  # TODO: Make dynamic
        years_to_try = [current_year, current_year - 1, current_year - 2]

        for year in years_to_try:
            bill_id_pattern = f"{bill_info.bill_prefix}-{bill_info.bill_number}-{year}"
            try:
                results = await self.vector_store.query(
                    query=f"{bill_info.bill_prefix} {bill_info.bill_number}",
                    top_k=3,
                    filter={"document_type": "bill", "bill_id": bill_id_pattern},
                )
                if results:
                    slug = results[0].metadata.get("slug")
                    if slug:
                        logger.info(
                            "Found bill slug from bill_id filter",
                            extracted_bill=bill_info.bill_id,
                            bill_id_pattern=bill_id_pattern,
                            matched_slug=slug,
                        )
                        return slug
            except Exception as e:
                logger.debug(f"Bill ID filter failed: {e}")

        # Phase 2: Semantic search with slug pattern matching
        # Build search query with jurisdiction name for better semantic matching
        jurisdiction_name = ""
        if bill_info.jurisdiction:
            # Map code back to full name for better semantic search
            code_to_name = {v: k for k, v in STATE_MAPPINGS.items() if len(k) > 2}
            jurisdiction_name = code_to_name.get(bill_info.jurisdiction, bill_info.jurisdiction)

        search_query = f"{jurisdiction_name} {bill_info.bill_prefix} {bill_info.bill_number}".strip()

        # Search for the bill in Pinecone
        filters = {"document_type": "bill"}

        results = await self.vector_store.query(
            query=search_query,
            top_k=10,
            filter=filters,
        )

        # Look for a result that matches our bill pattern
        slug_pattern = bill_info.slug_pattern
        for result in results:
            slug = result.metadata.get("slug", "")
            bill_id = result.metadata.get("bill_id", "")

            # Check if the slug contains our bill pattern
            if re.search(slug_pattern, slug, re.IGNORECASE):
                logger.info(
                    "Found bill slug from semantic search",
                    extracted_bill=bill_info.bill_id,
                    matched_slug=slug,
                )
                return slug

            # Also check bill_id metadata (normalize for comparison)
            if bill_id:
                normalized_bill_id = bill_id.lower().replace("-", "").replace(" ", "")
                if bill_info.bill_id.lower() in normalized_bill_id:
                    slug = result.metadata.get("slug")
                    if slug:
                        logger.info(
                            "Found bill slug from bill_id match",
                            extracted_bill=bill_info.bill_id,
                            matched_slug=slug,
                        )
                        return slug

        logger.debug(
            "Could not find slug for extracted bill",
            bill_info=bill_info.bill_id,
            jurisdiction=bill_info.jurisdiction,
        )
        return None

    def _build_filters(self, page_context: PageContext, query: str | None = None) -> dict:
        """
        Build Pinecone filters from page context and query analysis.

        Args:
            page_context: The current page context
            query: Optional query text for bill extraction (used when page_context is general)

        Returns:
            Filter dictionary for Pinecone query
        """
        filters = {}

        # For bills, use webflow_id as the filter (present in both summary and PDF chunks)
        if page_context.type == "bill":
            if page_context.webflow_id:
                filters["webflow_id"] = page_context.webflow_id
            # Fallback to slug if no webflow_id (only matches summary chunks, not PDFs)
            elif page_context.slug:
                filters["slug"] = page_context.slug
        elif page_context.type == "legislator" and page_context.id:
            filters["legislator_id"] = page_context.id

        # Note: jurisdiction filter removed as it's stored as Webflow ID, not code

        logger.info(
            "Built retrieval filters",
            page_type=page_context.type,
            webflow_id=page_context.webflow_id,
            slug=page_context.slug,
            filters=filters,
        )

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
