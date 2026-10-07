"""RAG retrieval orchestration service."""

import re
from dataclasses import dataclass, field

import structlog

from votebot.api.schemas.chat import PageContext
from votebot.config import Settings, get_settings
from votebot.services.bill_versions import BillVersionService, current_version
from votebot.services.vector_store import SearchResult, VectorStoreService
from votebot.services.webflow_lookup import WebflowLookupService
from votebot.utils.ddp_urls import ddp_bill_url_from_metadata
from votebot.utils.intent import detect_version_request

logger = structlog.get_logger()

# Lazy import to avoid circular imports
_federal_cache = None


def _get_federal_cache():
    """Get the federal legislator cache (lazy loaded)."""
    global _federal_cache
    if _federal_cache is None:
        from votebot.utils.federal_legislator_cache import get_federal_cache
        _federal_cache = get_federal_cache()
    return _federal_cache


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


# Two-letter codes that are also ordinary words, so only the capitalised form names a jurisdiction.
CODES_THAT_ARE_WORDS = frozenset({"us", "ma", "al"})

# How many chunks the "which bill is this?" lookup reads. A bill has many chunks, so a small top_k
# can be filled by one session's and never see the other's, defeating the ambiguity check.
BILL_LOOKUP_TOP_K = 100

# Live "what changed" diffs together are cut to this much (in chunks of the second size), so a
# whole-bill rewrite, or several named versions, cannot flood the prompt.
DIFF_MAX_CHARS = 12000
DIFF_CHUNK_CHARS = 4000

DIFF_UNAVAILABLE_NOTE = (
    "Version-change data could not be retrieved from the live records right now. Do not describe what "
    "changed between versions; tell the user you could not retrieve it just now."
)
DIFF_NONE_NOTE = (
    "No stored comparison exists for the requested version in the live records (it may be the bill's first "
    "version, or the version before it has no saved text). Say so; do not infer what changed."
)


def diff_scope_note(shown) -> str:
    """Names the exact version pairs the live diffs in this prompt compare, so the answer cannot claim more.

    api-v3 may hold a diff only for the latest steps of a bill, so "first version to latest" can be
    asked while one comparison was read; without this the answer says it covered the whole history.
    `shown` is a list of (VersionDiff, characters shown or None when the whole diff is in the prompt).
    """
    parts = []
    for diff, shown_chars in shown:
        before = diff.from_note or "the version before it (its name is not recorded)"
        detail = ", ".join(x for x in (diff.stage, diff.date) if x)
        label = f"{before} -> {diff.note or 'this version'}" + (f" ({detail})" if detail else "")
        if shown_chars is not None:
            label += f" [only the first {shown_chars} of {len(diff.text)} characters of this diff were read]"
        parts.append(label)
    heading = ""
    if len(shown) == 1:
        diff = shown[0][0]
        before = diff.from_note or "the version before it"
        # The model wrote its own heading ("Earliest to Latest Version") over a body that was accurate, in 2 of 5 live
        # runs after the wording rule alone, so with one comparison the heading is given to it.
        heading = f'Start your answer with exactly this heading: "## What changed: {before} -> {diff.note or "this version"}". '
    return (
        "The live version records give only these comparison(s) for this question: " + "; ".join(parts) + ". "
        + heading +
        "Name exactly these versions when you describe what changed from them, and do not present a comparison "
        "of any other pair of versions as if you had read it. If the user asked about a longer span (for example "
        "from the first version to the latest), say that only these comparison(s) were read and that earlier steps "
        "were not compared. If a diff was only partly read, say the summary covers only that part."
    )


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
    # Canonical-id index only: the `document_id` of the bill's current version, so the prompt
    # builder can mark that version "current". Looked up per request, never stored on vectors.
    current_document_id: str | None = None
    # Things the model must be told about this retrieval (e.g. live version data could not be read),
    # shown ahead of the sources so it does not answer from memory.
    notes: list[str] = field(default_factory=list)


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
        self.bill_versions = BillVersionService(self.settings)
        self.config = RetrievalConfig(
            max_chunks=self.settings.max_retrieval_chunks,
            similarity_threshold=self.settings.similarity_threshold,
        )

    @property
    def _ocd_mode(self) -> bool:
        """True when the configured index is keyed by `ocd_bill_id` rather than `webflow_id`.

        One setting decides both (`Settings.bill_filter_key`, derived from the index name), so
        rolling back to the legacy index needs no second change.
        """
        return self.settings.bill_filter_key == "ocd_bill_id"

    def _identity_filter(self, filters: dict) -> dict:
        """The part of `filters` that pins a bill, for follow-up queries on other document types."""
        key = self.settings.bill_filter_key
        if filters.get(key):
            return {key: filters[key]}
        if not self._ocd_mode and filters.get("slug"):  # ocd-keyed vectors carry no slug
            return {"slug": filters["slug"]}
        return {}

    async def _version_scope(self, ocd_bill_id: str, query: str) -> tuple[str | None, dict]:
        """(current version's document_id, metadata filter for the bill's versioned documents).

        The filter keeps a normal question on the CURRENT version, so one version of the text is
        in context instead of every version blended together. A query that names a stage ("as
        introduced", "the engrossed version") or a date asks for those versions instead. If
        api-v3 cannot say which version is current, no version filter is applied: the bill's
        chunks come back from every version, each labelled with its own.
        """
        current = current_version(await self.bill_versions.get_versions(ocd_bill_id))
        current_id = current.document_id if current else None

        requested = detect_version_request(query)
        if requested:
            version_filter: dict = {}
            if requested.stages:
                version_filter["version_stage"] = {"$in": list(requested.stages)}
            if requested.dates:
                version_filter["version_date"] = {"$in": list(requested.dates)}
            return current_id, version_filter
        if current_id:
            return current_id, {"document_id": current_id}
        return None, {}

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
        if page_context.type == "general" and self._ocd_mode:
            bill_info = self._extract_bill_from_query(query)
            if bill_info:
                upgraded = await self._lookup_ocd_bill_context(bill_info, page_context)
                if upgraded:
                    effective_context = upgraded
        elif page_context.type == "general":
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

        # Legislators are not embedded in the canonical-id index (their facts come live from api-v3,
        # see the agent): nothing to search, and no Webflow lookup to resolve an id with.
        if effective_context.type == "legislator" and self._ocd_mode:
            return RetrievalResult(chunks=[], query_used=query, filters_applied={}, total_retrieved=0)

        # For legislator pages with slug but no OpenStates ID, resolve via Webflow CMS
        if (
            effective_context.type == "legislator"
            and not effective_context.id
            and effective_context.slug
        ):
            resolved_id = await self._resolve_legislator_id(effective_context)
            if resolved_id:
                effective_context = PageContext(
                    type=effective_context.type,
                    id=resolved_id,
                    slug=effective_context.slug,
                    title=effective_context.title,
                    jurisdiction=effective_context.jurisdiction,
                    webflow_id=effective_context.webflow_id,
                    url=effective_context.url,
                )

        # A bill page with no ocd_bill_id cannot be isolated in a canonical-id index. Searching
        # without the filter would answer about whichever bills match the words, so return no
        # context (the agent falls back to its live OpenStates tool) and say so loudly.
        if effective_context.type == "bill" and self._ocd_mode and not effective_context.ocd_bill_id:
            logger.warning(
                "Bill page context has no ocd_bill_id; skipping retrieval rather than searching every bill",
                slug=effective_context.slug,
                webflow_id=effective_context.webflow_id,
            )
            return RetrievalResult(chunks=[], query_used=query, filters_applied={}, total_retrieved=0)

        # Build filters based on effective context
        filters = self._build_filters(effective_context, query)

        logger.info(
            "Starting retrieval",
            query_length=len(query),
            page_type=effective_context.type,
            filters=filters,
        )

        # For bill queries, use multi-phase retrieval to prioritize legislative text
        current_document_id = None
        notes: list[str] = []
        if effective_context.type == "bill":
            version_filter: dict = {}
            if self._ocd_mode:
                current_document_id, version_filter = await self._version_scope(
                    effective_context.ocd_bill_id, query
                )
            final_results = await self._retrieve_bill_with_text_priority(
                query=query,
                filters=filters,
                max_chunks=max_chunks,
                page_context=effective_context,
                version_filter=version_filter,
                notes=notes,
            )
        elif effective_context.type == "organization" or self._is_organization_query(query):
            # Organization-focused retrieval: prioritize org documents
            final_results = await self._retrieve_organization_priority(
                query=query,
                filters=filters,
                max_chunks=max_chunks,
            )
        else:
            # Standard retrieval for non-bill queries.
            # A general page that names a jurisdiction (and session) gets that jurisdiction's bill
            # text and votes, and no other jurisdiction's. Only those two types are scoped:
            # legislators and organizations carry no session_code and must stay retrievable, so
            # the unscoped query simply leaves the two bill types to the scoped one.
            scope = self._legislative_scope(effective_context)
            bill_types = ["bill-text", "bill-votes"]  # kept out of the unscoped query either way
            scoped_types = ["bill-text"] if self._ocd_mode else bill_types  # votes are not embedded there: do not ask
            base_filter = {**filters, "document_type": {"$nin": bill_types}} if scope else filters
            results = await self.vector_store.query(
                query=query,
                top_k=max_chunks * 2,
                filter=base_filter if base_filter else None,
            )
            if scope:
                scoped = await self.vector_store.query(
                    query=query,
                    top_k=max_chunks,
                    filter={**scope, "document_type": {"$in": scoped_types}},
                )
                results = scoped + results

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
            chunks=self._link_to_ddp_pages(final_results),
            query_used=query,
            filters_applied=filters,
            total_retrieved=len(final_results),
            current_document_id=current_document_id,
            notes=notes,
        )

    async def _retrieve_bill_with_text_priority(
        self,
        query: str,
        filters: dict,
        max_chunks: int,
        page_context: PageContext | None = None,
        version_filter: dict | None = None,
        notes: list[str] | None = None,
    ) -> list[SearchResult]:
        """
        Retrieve bill content with priority for actual legislative text.

        Phase 1: Get bill-text (PDF/legislative text) with the bill identity filter
        Phase 2: Get bill summaries with the bill identity filter
        Phase 3: Get bill-history (no webflow_id in metadata) using semantic search

        The identity filter is `webflow_id` on the legacy index and `ocd_bill_id` on the
        canonical-id index (see `Settings.bill_filter_key`).

        Args:
            query: The search query
            filters: Base filters (webflow_id or ocd_bill_id)
            max_chunks: Maximum chunks to return
            page_context: Page context with bill info for enhanced history search
            version_filter: Canonical-id index only. Metadata filter selecting which version(s) of
                the bill's text (and, for "what changed", diffs) to retrieve; see `_version_scope`

        Returns:
            List of SearchResult prioritizing legislative text
        """
        # Phase 1: Get legislative text chunks (document_type="bill-text")
        text_filters = {**filters, **(version_filter or {}), "document_type": "bill-text"}
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

        # Phase 3: Legislative history — REMOVED
        # Bill status and action history now comes exclusively from live OpenStates
        # API via the bill_votes tool. Stale bill-history chunks from Pinecone were
        # causing the LLM to report outdated status information (e.g., HR 7147 incident).
        history_results = []

        # Phase 4a: Get organization positions if query is about org support/opposition
        org_keywords = [
            "organization", "organizations", "org ", "orgs ",
            "who supports", "who support", "who opposes", "who oppose",
            "which groups", "which organizations",
            "support", "oppose", "backed", "backs", "endorses", "against",
        ]
        query_lower = query.lower()
        is_org_query = any(kw in query_lower for kw in org_keywords)

        # Phase 5 detection — evaluated early so it influences final result ordering.
        # Extends the canonical CHANGELOG_KEYWORDS with retrieval-only terms
        # ("amendment"/"amended") that broaden recall without polluting analytics.
        from votebot.utils.intent import CHANGELOG_KEYWORDS
        _changelog_retrieval_keywords = CHANGELOG_KEYWORDS + ["amendment", "amended"]
        is_changelog_query = any(kw in query_lower for kw in _changelog_retrieval_keywords)

        org_results = []
        bill_org_results = []

        if is_org_query:
            # Phase 4a-i: Search the bill's OWN chunks for org position content
            org_position_query = "organization positions supporting opposing this bill"
            bill_org_filters = {**filters, "document_type": "bill"}
            bill_org_results = await self.vector_store.query(
                query=org_position_query,
                top_k=3,
                filter=bill_org_filters,
            )
            bill_org_results = [
                r for r in bill_org_results
                if r.score >= self.config.similarity_threshold
            ]

            # Prioritize chunks with actual org position markers
            org_position_markers = ["organization positions", "organizations supporting", "organizations opposing"]
            bill_org_relevant = [r for r in bill_org_results if any(m in (r.content or "").lower() for m in org_position_markers)]
            bill_org_other = [r for r in bill_org_results if r not in bill_org_relevant]
            bill_org_results = bill_org_relevant + bill_org_other

            logger.info("Bill text retrieval phase 4a-i (bill org positions)",
                        bill_org_chunks_found=len(bill_org_results), bill_org_relevant=len(bill_org_relevant))

            remaining_org_slots = max(2, max_chunks - len(text_results) - len(summary_results) - len(history_results) - len(bill_org_results))

            # Build query combining bill info with org-related terms
            # Use the bill slug/title to find org chunks that reference this bill
            org_query = query
            if page_context:
                bill_title = page_context.title or ""
                bill_slug = page_context.slug or ""
                # Use slug keywords for better matching (org chunks contain bill DDP URLs with slug)
                slug_words = bill_slug.replace("-", " ") if bill_slug else ""
                if bill_title or slug_words:
                    org_query = f"{bill_title} {slug_words} support oppose bill positions"

            org_results = await self.vector_store.query(
                query=org_query,
                top_k=remaining_org_slots * 2,
                filter={"document_type": "organization"},
            )
            org_results = [
                r for r in org_results if r.score >= self.config.similarity_threshold
            ]

            # Post-filter: prioritize org chunks that actually mention this bill
            if page_context and org_results:
                bill_slug = (page_context.slug or "").lower()
                bill_title_lower = (page_context.title or "").lower()
                # Separate chunks that reference this specific bill from generic ones
                relevant = []
                other = []
                for r in org_results:
                    content_lower = (r.content or "").lower()
                    if (bill_slug and bill_slug in content_lower) or \
                       (bill_title_lower and bill_title_lower in content_lower):
                        relevant.append(r)
                    else:
                        other.append(r)
                org_results = relevant + other
                logger.info(
                    "Org results filtered by bill reference",
                    relevant_count=len(relevant),
                    other_count=len(other),
                )

            logger.info(
                "Bill text retrieval phase 4a (organizations)",
                org_chunks_found=len(org_results),
                org_query_preview=org_query[:50],
            )

        # Phase 4b: Get vote records if query is about voting
        vote_keywords = ["vote", "voted", "voting", "votes", "who supported", "who opposed", "pass", "passed", "fail", "failed"]
        is_vote_query = any(kw in query_lower for kw in vote_keywords)

        # Also detect follow-up questions about legislators when on a bill page
        # e.g., "how about rick scott?", "what about senator cruz?", "and rubio?"
        is_legislator_followup = False
        extracted_name = ""
        legislator_person_id = ""

        if page_context and page_context.type == "bill":
            followup_patterns = ["how about", "what about", "how did", "and ", "what did"]
            legislator_indicators = ["senator", "rep ", "representative", "congressman", "congresswoman"]

            has_followup_pattern = any(p in query_lower for p in followup_patterns)
            has_legislator_word = any(l in query_lower for l in legislator_indicators)

            # Extract potential name - works with both capitalized and lowercase
            # Filter out common query words to find potential names
            common_words = {"how", "did", "what", "about", "the", "this", "vote", "voted",
                          "on", "and", "senator", "rep", "representative", "congressman",
                          "congresswoman", "bill", "it", "they", "their", "a", "an", "for"}
            words = [w.strip("?.,!") for w in query.split()]
            potential_name_parts = [w for w in words if w.lower() not in common_words and len(w) > 1]

            # Try to find a legislator name in the federal cache
            if potential_name_parts and has_followup_pattern:
                try:
                    cache = _get_federal_cache()
                    # Try different combinations of potential name parts
                    # First try all parts together, then try individual parts
                    name_candidates = [
                        " ".join(potential_name_parts),  # "ashley moody"
                        potential_name_parts[-1] if potential_name_parts else "",  # "moody" (last name)
                    ]
                    # Also try title-cased versions
                    name_candidates.extend([n.title() for n in name_candidates])

                    for candidate in name_candidates:
                        if not candidate:
                            continue
                        cached_info = cache.lookup_with_info(candidate)
                        if cached_info:
                            extracted_name = cached_info.get("name", candidate)
                            legislator_person_id = cached_info.get("person_id", "")
                            is_legislator_followup = True
                            logger.info(
                                "Found legislator in federal cache",
                                query_name=candidate,
                                matched_name=extracted_name,
                                person_id=legislator_person_id,
                            )
                            break
                except Exception as e:
                    logger.warning("Failed to lookup legislator in cache", error=str(e))

            # Fall back to original detection if no cache match
            if not is_legislator_followup:
                # Check for proper name pattern (capitalized words)
                name_words = [w for w in query.split() if len(w) > 2 and w[0].isupper()]
                has_potential_name = len(name_words) > 0
                is_legislator_followup = has_followup_pattern and (has_legislator_word or has_potential_name)

            if is_legislator_followup:
                logger.info(
                    "Detected legislator follow-up query on bill page",
                    query_preview=query[:50],
                    extracted_name=extracted_name,
                )

        vote_results = []
        legislator_votes_results = []

        # If we haven't extracted a name yet but this is a legislator follow-up, try again
        if is_legislator_followup and not extracted_name and page_context:
            # Extract names (capitalized words that aren't common words)
            common_words = {"how", "did", "what", "about", "the", "this", "vote", "on", "and", "senator", "rep", "representative"}
            name_parts = [w for w in query.split() if len(w) > 1 and w[0].isupper() and w.lower() not in common_words]
            if name_parts:
                extracted_name = " ".join(name_parts)
                logger.info("Extracted legislator name for vote search", name=extracted_name)

                # Look up legislator's OpenStates person ID from federal cache
                try:
                    cache = _get_federal_cache()
                    cached_info = cache.lookup_with_info(extracted_name)
                    if cached_info:
                        legislator_person_id = cached_info.get("person_id", "")
                        logger.info(
                            "Found legislator in federal cache",
                            name=extracted_name,
                            person_id=legislator_person_id,
                        )
                except Exception as e:
                    logger.warning("Failed to lookup legislator in cache", error=str(e))

        # Votes (bill-votes, legislator-votes) are not embedded in the canonical-id index; they are read
        # live by the agent's bill lookup, so neither is searched there.
        votes_embedded = not self._ocd_mode

        # If we have a legislator person ID, query for their legislator-votes document directly
        if legislator_person_id and votes_embedded:
            person_uuid = legislator_person_id.replace("ocd-person/", "")
            doc_id_prefix = f"legislator-votes-{person_uuid}"

            # Query for legislator-votes documents with this person ID
            legislator_votes_results = await self.vector_store.query(
                query=f"{extracted_name} voting record votes",
                top_k=5,
                filter={"document_type": "legislator-votes"},
            )
            # Filter to only include results for this specific legislator
            legislator_votes_results = [
                r for r in legislator_votes_results
                if r.metadata.get('document_id') and person_uuid in r.metadata.get('document_id', '')
            ]
            logger.info(
                "Queried for legislator-votes document",
                person_id=legislator_person_id,
                results_found=len(legislator_votes_results),
            )

        # For vote queries OR legislator follow-ups on bill pages, get vote data
        if (is_vote_query or is_legislator_followup) and votes_embedded:
            vote_query = query
            if page_context:
                bill_id = page_context.id or ""
                bill_title = page_context.title or ""
                if bill_id or bill_title:
                    vote_query = f"{bill_id} {bill_title} vote voting record".strip()

            # Apply filters to get the correct bill's votes
            vote_filters = {"document_type": "bill-votes", **self._identity_filter(filters)}

            # Always request at least 5 vote chunks for vote queries
            vote_top_k = max(5, max_chunks)
            vote_results = await self.vector_store.query(
                query=vote_query,
                top_k=vote_top_k,
                filter=vote_filters,
            )
            vote_results = [
                r for r in vote_results if r.score >= self.config.similarity_threshold
            ]

            # If we're looking for a specific legislator, prioritize chunks containing their name
            if extracted_name and vote_results:
                name_lower = extracted_name.lower()
                # Separate chunks that contain the name vs those that don't
                with_name = [r for r in vote_results if name_lower in (r.content or "").lower()]
                without_name = [r for r in vote_results if name_lower not in (r.content or "").lower()]
                # Prioritize chunks with the name
                vote_results = with_name + without_name
                logger.info(
                    "Re-ranked vote results for legislator name",
                    name=extracted_name,
                    chunks_with_name=len(with_name),
                    chunks_without_name=len(without_name),
                )

            logger.info(
                "Bill text retrieval phase 4 (votes)",
                vote_chunks_found=len(vote_results),
                vote_query_preview=vote_query[:50],
            )

        # Phase 5: what changed between versions (changelog intent only)
        changelog_results = []
        changelog_filter = None
        if is_changelog_query and self._ocd_mode:
            # Canonical-id index: diffs are not embedded; the change is api-v3's stored diff, read
            # live under the same version scope as the text (the current version's diff unless a
            # version is named) and labelled with both versions.
            changelog_results = await self._live_version_diffs(filters.get("ocd_bill_id"), query, notes)
        elif is_changelog_query and filters.get("webflow_id"):
            changelog_filter = {"document_type": "bill-changelog", "webflow_id": filters["webflow_id"]}
        if changelog_filter:
            changelog_results = await self.vector_store.query(
                query=query,
                top_k=3,
                filter=changelog_filter,
            )
            changelog_results = [
                r for r in changelog_results if r.score >= self.config.similarity_threshold
            ]
            logger.info(
                "Bill text retrieval phase 5 (changelog)",
                changelog_chunks_found=len(changelog_results),
            )

        # Combine results based on query type
        if is_changelog_query and changelog_results:
            # For changelog queries, surface changelog docs first, then current text
            combined = changelog_results + text_results + summary_results + history_results + vote_results + org_results
        elif (is_vote_query or is_legislator_followup) and (legislator_votes_results or vote_results):
            # For vote queries and legislator follow-ups, prioritize:
            # 1. Legislator-votes documents (if we found the specific legislator)
            # 2. Bill-votes documents
            # 3. Other content
            combined = legislator_votes_results + vote_results + text_results + summary_results + history_results + org_results
        elif is_org_query and (bill_org_results or org_results):
            # For org queries, prioritize bill's own org positions, then standalone org docs
            combined = bill_org_results + org_results + summary_results + text_results + history_results + vote_results
        else:
            # Default: legislative text first, then summaries, then history, then votes, then orgs
            combined = text_results + summary_results + history_results + vote_results + org_results

        # Deduplicate
        if self.config.deduplicate:
            combined = self._deduplicate(combined)

        # If we still don't have results, try without document_type filter. The version scope
        # stays: broadening a current-version (or named-version) search to every version would
        # answer from a version the user did not ask about, with nothing to show it happened.
        if not combined:
            logger.info("No typed results, falling back to unfiltered query")
            fallback_filter = {**filters, **(version_filter or {})}
            all_results = await self.vector_store.query(
                query=query,
                top_k=max_chunks * 2,
                filter=fallback_filter if fallback_filter else None,
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
            # Whole words only: "al" is not in "actually", nor "ma" in "summarize". Codes that are
            # also ordinary words ("tell us about HB 5") count only when written in capitals.
            haystack = query if name in CODES_THAT_ARE_WORDS else query_lower
            needle = name.upper() if name in CODES_THAT_ARE_WORDS else name
            if re.search(rf"\b{re.escape(needle)}\b", haystack):
                jurisdiction = code
                break

        # Patterns to match bill identifiers
        # Pattern 1: Standard bill format (HB 363, SB 123, HR 1004, S 302, HJR 4210, SJR 100)
        pattern1 = r'\b(H\.?J\.?R\.?|S\.?J\.?R\.?|H\.?C\.?R\.?|S\.?C\.?R\.?|H\.?B\.?|S\.?B\.?|H\.?R\.?|S\.?|H\.?J\.?|S\.?J\.?)\s*(\d+)\b'

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

    async def _resolve_legislator_id(self, page_context: PageContext) -> str | None:
        """
        Resolve a legislator's OpenStates ID from slug via Webflow CMS lookup.

        When the page context has a slug but no OpenStates ID (common for Webflow
        pages), look up the Webflow CMS item to get the openstatesid field.

        Args:
            page_context: Page context with slug but no id

        Returns:
            OpenStates person ID if found, None otherwise
        """
        slug = page_context.slug
        if not slug:
            return None

        try:
            webflow_service = WebflowLookupService()
            result = await webflow_service.get_legislator_details(slug=slug)
            if result.found and result.openstates_id:
                logger.info(
                    "Resolved legislator OpenStates ID from slug",
                    slug=slug,
                    openstates_id=result.openstates_id,
                    name=result.name,
                )
                return result.openstates_id
            else:
                logger.warning(
                    "Could not resolve legislator OpenStates ID from slug",
                    slug=slug,
                    found=result.found,
                )
        except Exception as e:
            logger.error(
                "Failed to resolve legislator ID from Webflow",
                slug=slug,
                error=str(e),
            )

        return None

    def _is_organization_query(self, query: str) -> bool:
        """Detect if the query is primarily about an organization."""
        query_lower = query.lower()

        # Patterns that indicate an org-focused query
        org_patterns = [
            "what type of organization",
            "what kind of organization",
            "tell me about",  # Often followed by org name
            "what bills does",  # "What bills does X support?"
            "what is ",  # "What is X?" could be org
            "who is ",
        ]

        # Check for org-related phrases combined with non-bill-number queries
        # If the query also contains a bill identifier, let the bill pipeline handle it
        has_bill_id = self._extract_bill_from_query(query) is not None
        if has_bill_id:
            return False

        # Strong org indicators
        strong_indicators = [
            "organization", "organisations", "nonprofit", "non-profit",
            "501(c)", "advocacy group", "committee",
        ]
        if any(ind in query_lower for ind in strong_indicators):
            return True

        # Check for org query patterns
        for pattern in org_patterns:
            if pattern in query_lower:
                return True

        return False

    async def _retrieve_organization_priority(
        self,
        query: str,
        filters: dict,
        max_chunks: int,
    ) -> list[SearchResult]:
        """
        Retrieve with priority for organization documents.

        Phase 1: Search organization documents semantically
        Phase 2: If a specific org is identified, fetch ALL its chunks
                 (bill positions may be in a different chunk than the header)
        Phase 3: Fill remaining slots with general results

        Args:
            query: The search query
            filters: Base filters
            max_chunks: Maximum chunks to return

        Returns:
            List of SearchResult prioritizing organization content
        """
        # Phase 1: Search organization documents (scoped by page context if available)
        org_filter = {"document_type": "organization"}
        for key in ("webflow_id", "broker_org_id", "slug"):
            if filters.get(key):
                org_filter[key] = filters[key]
                break

        org_results = await self.vector_store.query(
            query=query,
            top_k=max_chunks,
            filter=org_filter,
        )
        org_results = [
            r for r in org_results if r.score >= self.config.similarity_threshold
        ]

        logger.info(
            "Organization retrieval phase 1",
            org_chunks_found=len(org_results),
        )

        # Phase 2: If top result is a specific org, fetch ALL chunks for that org
        # This ensures bill positions (which may be in a different chunk) are included
        if org_results:
            top_doc_id = org_results[0].metadata.get("document_id", "")
            if top_doc_id:
                # Search for more chunks with a query focused on bill positions
                bill_position_results = await self.vector_store.query(
                    query=f"bill positions supported opposed bills",
                    top_k=5,
                    filter={"document_type": "organization", "document_id": top_doc_id},
                )
                # Add any new chunks not already in results
                existing_ids = {r.id for r in org_results}
                for r in bill_position_results:
                    if r.id not in existing_ids:
                        org_results.append(r)
                        existing_ids.add(r.id)

                logger.info(
                    "Organization retrieval phase 2 (all chunks)",
                    org_doc_id=top_doc_id,
                    total_org_chunks=len(org_results),
                )

        # Phase 3: Fill remaining slots with general results
        remaining_slots = max_chunks - len(org_results)
        general_results = []

        if remaining_slots > 0:
            general_results = await self.vector_store.query(
                query=query,
                top_k=remaining_slots * 2,
                filter=filters if filters else None,
            )
            general_results = [
                r for r in general_results if r.score >= self.config.similarity_threshold
            ]

        combined = org_results + general_results

        if self.config.deduplicate:
            combined = self._deduplicate(combined)

        return combined[:max_chunks]

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

        if page_context.type == "bill" and self._ocd_mode:
            # Canonical-id index: every document of a bill carries its ocd_bill_id, and nothing
            # else identifies it (no slug, no webflow_id), so there is deliberately no fallback.
            if page_context.ocd_bill_id:
                filters["ocd_bill_id"] = page_context.ocd_bill_id
        # For bills, use webflow_id as the filter (present in both summary and PDF chunks)
        elif page_context.type == "bill":
            if page_context.webflow_id:
                filters["webflow_id"] = page_context.webflow_id
            # Fallback to slug if no webflow_id (only matches summary chunks, not PDFs)
            elif page_context.slug:
                filters["slug"] = page_context.slug
        elif page_context.type == "legislator":
            if page_context.id:
                filters["legislator_id"] = page_context.id
            elif page_context.webflow_id:
                filters["webflow_id"] = page_context.webflow_id
            elif page_context.slug:
                filters["slug"] = page_context.slug
        elif page_context.type == "organization" and self._ocd_mode:
            # Canonical-id index (SYNC-91): organization vectors carry the broker's organization id
            # (a number) and the broker's slug, never a webflow_id. A Webflow-style context sends
            # the slug as `id`, so only an all-digit id is taken for the broker id.
            if page_context.id and page_context.id.isdigit():
                filters["broker_org_id"] = page_context.id
            elif page_context.slug:
                filters["slug"] = page_context.slug
        elif page_context.type == "organization":
            if page_context.webflow_id:
                filters["webflow_id"] = page_context.webflow_id
            elif page_context.slug:
                filters["slug"] = page_context.slug

        # Note: jurisdiction filter removed as it's stored as Webflow ID, not code

        logger.info(
            "Built retrieval filters",
            page_type=page_context.type,
            webflow_id=page_context.webflow_id,
            ocd_bill_id=page_context.ocd_bill_id,
            slug=page_context.slug,
            filters=filters,
        )

        return filters

    async def _live_version_diffs(
        self, ocd_bill_id: str | None, query: str, notes: list[str] | None = None
    ) -> list[SearchResult]:
        """The stored diffs for a "what changed" question, as `bill-version-diff` chunks.

        Read from api-v3 (nothing is searched in the index). Together they are cut to
        `DIFF_MAX_CHARS`, in chunks, with a note saying so. When api-v3 cannot answer, or has no
        stored diff for the version, `notes` gets a message saying which, so the answer says it
        cannot show the change instead of guessing.
        """
        if not ocd_bill_id:
            return []
        requested = detect_version_request(query)
        try:
            diffs = await self.bill_versions.get_diffs(
                ocd_bill_id,
                stages=tuple(requested.stages) if requested else (),
                dates=tuple(requested.dates) if requested else (),
            )
        except Exception as e:  # noqa: BLE001 -- a lookup problem must cost the diff, not the answer
            logger.warning("Could not read version diffs from api-v3", ocd_bill_id=ocd_bill_id, error=str(e))
            diffs = None
        if diffs is None:
            if notes is not None:
                notes.append(DIFF_UNAVAILABLE_NOTE)
            return []
        if not diffs:
            if notes is not None:
                notes.append(DIFF_NONE_NOTE)
            return []
        chunks: list[SearchResult] = []
        shown: list = []
        remaining, omitted, truncated = DIFF_MAX_CHARS, 0, 0
        for diff in diffs:
            if remaining <= 0:
                omitted += 1
                continue
            text = diff.text[:remaining]
            shown.append((diff, len(text) if len(diff.text) > remaining else None))
            if len(diff.text) > remaining:
                truncated += 1
                text += f"\n[Diff truncated: first {len(text)} of {len(diff.text)} characters]"
            remaining -= len(diff.text[:remaining])
            for n, piece in enumerate(text[i : i + DIFF_CHUNK_CHARS] for i in range(0, len(text), DIFF_CHUNK_CHARS)):
                chunks.append(
                    SearchResult(
                        id=f"bill-version-diff:{ocd_bill_id}:{diff.document_id}-live-{n}",
                        content=piece,
                        score=1.0,
                        metadata={
                            "document_type": "bill-version-diff",
                            "source": "OpenStates (live)",
                            "ocd_bill_id": ocd_bill_id,
                            "document_id": diff.document_id,
                            "version_note": diff.note,
                            "version_date": diff.date,
                            "version_stage": diff.stage,
                            **({"from_version_note": diff.from_note} if diff.from_note else {}),
                            **({"from_document_id": diff.from_document_id} if diff.from_document_id else {}),
                        },
                    )
                )
        if notes is not None:
            notes.append(diff_scope_note(shown))
        if omitted and notes is not None:
            notes.append(f"The changes of {omitted} further matching version(s) are omitted for length; say so if asked.")
        logger.info(
            "Live version diffs", ocd_bill_id=ocd_bill_id, versions=len(diffs), chunks=len(chunks),
            truncated=truncated, omitted=omitted,
        )
        return chunks

    def _link_to_ddp_pages(self, chunks: list[SearchResult]) -> list[SearchResult]:
        """Point bill chunks at their page on our site (canonical-id index, `DDP_SITE_BASE_URL` set).

        The chunk's `url` becomes the page URL, so the source header the model is told to cite, the
        citation match and the citation's link all agree; the legislature's own URL is kept as
        `source_url` (the page links to it). Anything else, or an unset base URL, is untouched.
        """
        base = self.settings.ddp_site_base_url
        if not (base and self._ocd_mode):
            return chunks
        linked = []
        for chunk in chunks:
            page_url = ddp_bill_url_from_metadata(base, chunk.metadata)
            if page_url:
                original = chunk.metadata.get("url")
                chunk = SearchResult(
                    id=chunk.id,
                    content=chunk.content,
                    score=chunk.score,
                    metadata={
                        **chunk.metadata,
                        "url": page_url,
                        **({"source_url": original} if original and not chunk.metadata.get("source_url") else {}),
                    },
                )
            linked.append(chunk)
        return linked

    def _legislative_scope(self, page_context: PageContext) -> dict:
        """Jurisdiction and session filter for a general page that names them (canonical-id index only).

        Vectors there carry the upper-case jurisdiction code as `jurisdiction` and the legislative
        session as `session_code`. Empty on the legacy index, where `jurisdiction` is a Webflow id.
        """
        if not self._ocd_mode or page_context.type != "general" or not page_context.jurisdiction:
            return {}
        scope = {"jurisdiction": page_context.jurisdiction.upper()}
        if page_context.session:
            scope["session_code"] = page_context.session
        return scope

    async def _lookup_ocd_bill_context(
        self, bill_info: ExtractedBillInfo, page_context: PageContext
    ) -> PageContext | None:
        """Canonical-id counterpart of `_lookup_bill_slug`: find the bill a general-page query names.

        Looks the bill up by its identifier (`gov_id`, e.g. "HB 363") within a jurisdiction, and a
        session when the page names one. Without a jurisdiction, or when the identifier matches more
        than one bill, no guess is made. Returns a bill `PageContext`, or None.
        """
        # The page's jurisdiction wins over one guessed from the query text
        jurisdiction = page_context.jurisdiction or bill_info.jurisdiction
        if not jurisdiction:
            return None
        lookup = {
            "document_type": "bill-text",
            "gov_id": f"{bill_info.bill_prefix} {bill_info.bill_number}",
            "jurisdiction": jurisdiction.upper(),
        }
        if page_context.session:
            lookup["session_code"] = page_context.session

        results = await self.vector_store.query(
            query=f"{bill_info.bill_prefix} {bill_info.bill_number}",
            top_k=BILL_LOOKUP_TOP_K,
            filter=lookup,
        )
        matches = [r for r in results if r.metadata.get("ocd_bill_id")]
        bill_ids = {r.metadata["ocd_bill_id"] for r in matches}
        if len(bill_ids) != 1:
            if bill_ids:
                # Every session has its own "HB 1". Session codes do not order reliably as text
                # (a special session "2026D" sorts after "2026"), so guessing could answer about
                # the wrong bill; a page that supplies its session removes the ambiguity.
                logger.info(
                    "Bill named in query matches several bills; not guessing",
                    gov_id=lookup["gov_id"],
                    candidates=len(bill_ids),
                )
            return None
        best = matches[0]
        logger.info(
            "Upgraded to bill context from query extraction (ocd_bill_id)",
            gov_id=lookup["gov_id"],
            ocd_bill_id=best.metadata["ocd_bill_id"],
        )
        return PageContext(
            type="bill",
            ocd_bill_id=best.metadata["ocd_bill_id"],
            title=lookup["gov_id"],
            jurisdiction=lookup["jurisdiction"],
            session=best.metadata.get("session_code") or page_context.session,
        )

    def _deduplicate(self, results: list[SearchResult]) -> list[SearchResult]:
        """
        Remove duplicate or near-duplicate chunks.

        Uses content hashing to identify duplicates. On the canonical-id index the key includes the
        version's `document_id`: bill versions share most of their text, and merging identical
        passages from different versions would erase which version a passage came from. The
        legacy index keeps its original content-only key.

        Args:
            results: List of search results

        Returns:
            Deduplicated list of search results
        """
        seen_content = set()
        deduplicated = []

        for result in results:
            # Create a simple hash of the content, scoped to the document (version) it came from
            document_id = result.metadata.get("document_id") if self._ocd_mode else None
            content_hash = (document_id, hash(result.content[:500]))

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
