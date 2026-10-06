---
name: votebot primitives & building blocks inventory
description: Catalog of every service, dataclass, helper, and convention in the codebase. Read at the start of every PLAN session before designing new shapes.
type: reference
---

# READ THIS FIRST — BEFORE DESIGNING NEW PRIMITIVES

Before sketching new dataclasses, retrieval phases, helpers, or conventions in any PLAN session, scan this file and grep the relevant module. The pattern to avoid: drafting a "new primitive" that duplicates something already at a known path.

```bash
grep -rn "class <Name>\|def <name>" src/votebot/
```

---

## Pinecone layer (`services/vector_store.py`)

- **`VectorStoreService`** — Pinecone client. Lazy-initialized. Methods:
  - `query(query, top_k, filter, include_metadata) -> list[SearchResult]`
  - `query_with_filter(query, document_type, bill_id, legislator_id, jurisdiction, top_k) -> list[SearchResult]`
  - `upsert_documents(documents: list[Document], batch_size=100) -> int`
  - `delete(ids=None, filter=None, delete_all=False) -> None`
  - `health_check() -> bool`
- **`Document`** — `id, content, metadata, embedding`
- **`SearchResult`** — `id, content, score, metadata`
- **`VectorStoreServiceFactory.get_instance()`** — singleton accessor

## Retrieval service (`core/retrieval.py`)

The single retrieval orchestrator. **Do not add raw Pinecone calls outside this module.**

- **`RetrievalService`** — multi-phase retrieval entry point:
  - `retrieve(query, page_context: PageContext, max_chunks) -> RetrievalResult` — routes to bill, org, or standard retrieval based on context
  - `_retrieve_bill_with_text_priority(query, filters, max_chunks, page_context) -> list[SearchResult]` — 5-phase bill retrieval:
    - Phase 1: `bill-text` + webflow_id
    - Phase 2: `bill` (CMS summary) + webflow_id
    - Phase 3: removed (stale bill-history)
    - Phase 4a: org positions (`bill` + `organization`)
    - Phase 4b: vote records (`bill-votes`, `legislator-votes`)
    - Phase 5: what changed, **only on changelog intent**. Legacy index: `bill-changelog` + webflow_id. Canonical-id index (VOTEBOT-10): `bill-version-diff` (api-v3's stored `diff_from_previous_version`, embedded verbatim) under the same version scope as the text
  - `_retrieve_organization_priority(query, filters, max_chunks) -> list[SearchResult]`
  - `_build_filters(page_context, query) -> dict` — builds Pinecone filter from page context; use this, never build filters inline. A bill is pinned by `webflow_id` on the legacy index and by `ocd_bill_id` on the canonical-id index (VOTEBOT-8). An organization is pinned by `webflow_id`/`slug` on the legacy index and by `broker_org_id` (an all-digit `page_context.id`, else the broker `slug`) on the canonical-id one (VOTEBOT-15, SYNC-91)
  - `_version_scope(ocd_bill_id, query) -> (current_document_id, version_filter)` — canonical-id index only. "Current" is **looked up** (via `BillVersionService`), never stored on vectors. Default filter `{"document_id": <current>}`; a query naming a stage/date gets `version_stage`/`version_date` `$in` filters instead; unknown current → no version filter (all versions, each labelled, and the formatter says no version is current). Applied to `bill-text`, `bill-version-diff` **and the "no typed results" fallback** — a version search is never widened to every version. A date counts as a version request only in a query that also says "version" or "draft"
  - `_ocd_mode` (property) — True when `settings.bill_filter_key == "ocd_bill_id"`. **The one switch**: it follows the index name, so there is no second setting to forget on rollback
  - `_identity_filter(filters) -> dict` — the bill-pinning part of a built filter, for follow-up queries on other document types (votes). Use it instead of reading `filters["webflow_id"]` directly
  - `_legislative_scope(page_context) -> dict` — `jurisdiction` (upper-case code) + `session_code` for a general page that names them; canonical-id index only. Governs `bill-text`/`bill-votes` **only**: the scoped query returns that jurisdiction's, and the unscoped query excludes those two types (`$nin`), so other jurisdictions' bills stay out while legislators/organizations (no `session_code`) stay in
  - `_lookup_ocd_bill_context(bill_info, page_context) -> PageContext | None` — canonical-id counterpart of `_lookup_bill_slug`: a bill named on a general page, found by `gov_id` + jurisdiction (+ session). **Declines to guess** when the name matches more than one bill (every session has its own "HB 1"; session codes do not sort reliably as text, e.g. "2026D" after "2026")
  - `_deduplicate(results) -> list[SearchResult]` — the key is `(metadata["document_id"], hash(content[:500]))`: bill versions share most of their text, and merging identical passages across versions would erase which version they came from
  - `retrieve_for_bill(query, bill_id, jurisdiction) -> RetrievalResult`
  - `retrieve_for_legislator(query, legislator_id, jurisdiction) -> RetrievalResult`
  - `retrieve_general(query, jurisdiction) -> RetrievalResult`
- **`RetrievalResult`** — `chunks, query_used, filters_applied, total_retrieved, current_document_id` (the bill's current version on the canonical-id index, for the prompt builder to mark "current"; None otherwise)
- **`RetrievalConfig`** — `max_chunks, similarity_threshold, use_hybrid_search, deduplicate`
- **`ExtractedBillInfo`** — `bill_prefix, bill_number, jurisdiction`. Properties: `bill_id`, `slug_pattern`
- **`HybridRetrievalService`** — subclass of `RetrievalService`; keyword search stub, not yet implemented

**Retrieval isolation rule**: `bill-text-history`, `bill-changelog` and `bill-version-diff` are invisible to all existing phases by design (explicit `document_type` filters). Only Phase 5 queries `bill-changelog` (legacy index) or `bill-version-diff` (canonical-id index), and only on changelog intent. Never add unfiltered fallback queries that could surface these types in normal responses.

## Intent classification (`utils/intent.py`)

Single source of truth for intent taxonomy and retrieval vocabulary.

- **`PrimaryIntent`** StrEnum — `BILL, LEGISLATOR, ORGANIZATION, GENERAL, OUT_OF_SCOPE`
- **`SubIntent`** StrEnum — `SUMMARY, SUPPORT_OPPOSITION, VOTE_HISTORY, STATUS, EXPLANATION, COMPARISON, CHANGELOG, VOTING_RECORD, CONTACT, BIO, DDP_SCORE, SPONSORED_BILLS, POSITIONS, INFO, BILL_ALIGNMENT, NAVIGATION, HOW_TO_VOTE, ABOUT_DDP, ISSUE_AREA, TEXT_EDITING, GREETING, OFF_TOPIC, META, CIVIC_ACTION, UNKNOWN`
- **`CHANGELOG_KEYWORDS: list[str]`** — canonical keyword list for changelog intent detection. **Single source of truth** — imported by `retrieval.py` for Phase 5 detection. `retrieval.py` extends it with `["amendment", "amended"]` for broader retrieval recall without polluting analytics.
- **`VERSION_STAGE_KEYWORDS`, `VersionRequest(stages, dates)`, `detect_version_request(query)`** (VOTEBOT-10) — how users name a bill version ("as introduced", "the engrossed version", a date), mapped to api-v3's `version_stage` labels (`introduced, amendment, chamber_passage, final_passage, enacted`; never `unknown`). Specific phrases only: a bare "introduced"/"amended" is a status question, and a date alone ("does it take effect March 4?") is not a version request. Extend the vocabulary here, not inline in retrieval
- **`VALID_RETRIEVAL_SOURCES: frozenset`** — controlled vocabulary for `document_type` values. Add new document types here AND in ddp-sync's document type table. Values: `bill, bill-text, bill-history, bill-votes, bill-changelog, bill-text-history, bill-version-diff, legislator, legislator-votes, organization, training`
- **`classify_primary_intent(page_type, message) -> str`**
- **`classify_sub_intent(primary_intent, message) -> str`**
- **`normalize_retrieval_sources(raw_sources) -> list[str]`** — maps unknown types to `"unknown"` with a warning; don't skip this

## LLM service (`services/llm.py`)

- **`LLMService`** — OpenAI Responses API + Chat Completions. Methods:
  - `complete(messages, system_prompt, max_tokens, temperature, enable_web_search, enable_bill_votes, bill_votes_service, previous_response_id) -> LLMResponse` — non-streaming; uses `_join_response_blocks()` for block-boundary whitespace fix. No `tools` param — tools are built internally via `_build_tools()` from the `enable_web_search`/`enable_bill_votes` flags
  - `complete_with_fallback(messages, system_prompt, max_tokens, temperature, rag_confidence, previous_response_id, page_context_type) -> LLMResponse` — wraps `complete()`, auto-enabling web search when `rag_confidence` is below threshold. Threshold varies by `page_context_type`: `web_search_legislator_confidence_threshold` / `web_search_organization_confidence_threshold` (both 0.7) for legislator/org pages, else `web_search_confidence_threshold` (0.5)
  - `stream(messages, system_prompt, max_tokens, temperature, enable_web_search) -> AsyncIterator[StreamChunk]` — routes to Responses API (web search on) or Chat Completions (web search off)
  - `health_check() -> bool`
- **`_join_response_blocks(response) -> str`** — module-level helper. Replaces `response.output_text` to fix SDK block-boundary whitespace loss (`"".join()` drops `\n\n` at block boundaries). **Do not use `response.output_text` directly.**
- **`LLMResponse`** — `content, tokens_used, model, finish_reason, web_search_used, web_citations, response_id, bill_votes_tool_used, bill_votes_result`
- **`StreamChunk`** ⚠️ — `text, done, web_search_used`. **Name collision**: `api/schemas/chat.py` also has a class called `StreamChunk` (the WebSocket wire format). When importing, always verify which module you're pulling from. Consider renaming one of them in a future cleanup.
- **`WebSearchCitation`** ⚠️ — `url, title, snippet`. **Near-duplicate**: `api/schemas/chat.py` has `WebCitation` with the same three fields as a Pydantic model. One is a dataclass (internal), one is the API schema (external). There is a translation step between them. Don't add a third web citation shape.
- **`BillVotesToolResult`** — tool call result shape from `get_bill_info`
- **`LLMServiceFactory.get_instance()`** — singleton

## Agent (`core/agent.py`)

- **`VoteBotAgent`** — orchestrates retrieval → augmentation → LLM → verification. Two entry points:
  - `process_message(message, session_id, page_context, navigation_context, conversation_history, channel, human_active, client_ip, user_agent, button, ...) -> AgentResult` — non-streaming (HTTP endpoint). `...` covers analytics-only kwargs (`visitor_id`, `conversation_id`, `session_message_index`, `conversation_message_index`, `entry_referrer`, `page_url`, `scroll_depth`, `time_on_page`) passed through to `_log_query`
  - `process_message_stream(...)` — same signature shape as `process_message` — streaming (WebSocket)
  - Note: both entry points check the button cache first (`_maybe_serve_from_button_cache`) and return/yield the cached response without calling the LLM if it's a cache hit. No `llm.stream()`/`llm.complete()` call occurs on cache hits.
- **`_resolve_bill_from_title(message, page_context=None) -> (identifier, jurisdiction)`** — a bill named by title rather than number. Legacy index: top `bill` summary (reads `bill_prefix`/`bill_number`). Canonical-id index: top `bill-text` chunk (reads `gov_id`; the `bill` type does not exist there), limited to a general page's jurisdiction/session via `RetrievalService._legislative_scope`. Score must exceed 0.7 on both.
- **`AgentResult`** — `response, citations, confidence, requires_human, tokens_used, retrieval_count, cached, web_search_used, web_citations, response_id, bill_votes_tool_used, bill_votes_tool_duration_ms, bill_votes_result`. No `metadata` field on this dataclass (that's on `StreamChunkData`/`ResponseMetadata` instead).
- **`StreamChunkData`** — `text, done, citations, metadata`. Only 4 fields. There is no confidence/requires_human/web_search_used/bill_votes_tool_used field on this dataclass or on `ResponseMetadata` — `api/routes/websocket.py` recomputes `confidence` (`calculate_confidence()`) and `requires_human` (`check_human_handoff()`) itself from the accumulated response text once `done=True`, independent of `StreamChunkData`.

## Prompts (`core/prompts.py`)

- **`SYSTEM_PROMPT_BASE`** — base system prompt. Contains the bullet-per-line instruction. **Do not duplicate formatting rules inline.**
- **`BILL_CONTEXT_PROMPT`** — bill page context; includes changelog guidance ("cite version transition explicitly; say so if no changelog available")
- **`LEGISLATOR_CONTEXT_PROMPT`**, **`ORGANIZATION_CONTEXT_PROMPT`**, **`GENERAL_CONTEXT_PROMPT`** — context-specific prompt sections
- **`VERSION_CONTEXT_PROMPT`** (VOTEBOT-10) — bill pages only, appended after `BILL_CONTEXT_PROMPT`: answer from the version marked current unless another is asked for, name the version for every claim, answer "what changed" from "Changes in ..." sources naming both versions, and say so if the asked-for version is not in the sources
- **`RAG_CONTEXT_TEMPLATE`** — wrapper for retrieved context injected into the prompt
- **`CITATION_INSTRUCTION`** / **`ENHANCED_CITATION_INSTRUCTION`** — citation formatting rules; `ENHANCED_CITATION_INSTRUCTION` gated on `settings.enhanced_citation_prompt`
- **`HUMAN_HANDOFF_PROMPT`** — human handoff detection instructions; always appended last in `build_system_prompt`
- **`CONFIDENCE_SCORING_PROMPT`** — confidence rubric text. Defined but **currently unused** — not appended anywhere in `build_system_prompt`. Confidence is instead computed by `_calculate_confidence`/`calculate_confidence` heuristics, not by prompting the LLM.
- **`build_system_prompt(page_type, page_info, include_rag_context, retrieved_context) -> str`** — assembles the full system prompt: `SYSTEM_PROMPT_BASE` + context-specific prompt + optional `RAG_CONTEXT_TEMPLATE` + citation instruction + `HUMAN_HANDOFF_PROMPT`. Always use this, never concatenate prompts manually
- **`format_retrieved_chunks(chunks: list[dict], current_document_id: str | None = None) -> str`** — formats retrieved chunks for RAG injection. Adds `**Version Change:** from → to` header for `bill-changelog` chunks (legacy). Chunks of a labelled bill version (`bill-text`/`bill-version-diff` with `document_id` and `version_note`/`version_stage`) are **grouped under one header** such as `## HB 1 · Engrossed · 2026-03-04 · current` (diffs: `## HB 1 · Changes in Engrossed · 2026-03-04 · from Introduced`), the group sitting where its first chunk ranked. When version groups are present but no version is current, a `NO_CURRENT_VERSION_NOTE` leads the context. Callers pass `retrieval_result.current_document_id`. **Use this, don't write inline formatters.**
- **`_build_ddp_url(metadata, doc_type) -> str | None`** — builds DDP citation URL from slug in metadata

## Webflow runtime lookup (`services/webflow_lookup.py`)

Bidirectional CMS fetch used at query time for RAG augmentation/verification. All lookup methods take `(webflow_id: str | None = None, slug: str | None = None)` — webflow_id preferred, slug is the fallback search.

**Not fully read-only**: `update_bill_fields()` / `update_bill_gov_url()` DO write to Webflow CMS (PATCH `.../items/{id}/live`), used by scheduler/sync scripts (`scripts/sync_bills.py` etc., outside `src/votebot/`) with the write-scoped `webflow_scheduler_api_key`. The runtime *retrieval/agent* path only ever calls the read methods below.

- **`WebflowLookupService`** — methods:
  - `get_bill_org_positions(webflow_id=None, slug=None) -> BillOrgPositionsResult` — org positions for a bill
  - `get_org_bill_positions(webflow_id=None, slug=None) -> OrgBillPositionsResult` — bills for an org
  - `get_bill_details(webflow_id=None, slug=None) -> BillDetailsResult` — bill metadata for dispute verification
  - `get_legislator_details(webflow_id=None, slug=None) -> LegislatorDetailsResult` — resolves slug → OpenStates ID + details
  - `get_org_details(webflow_id=None, slug=None) -> OrgDetailsResult`
  - `update_bill_fields(webflow_id, field_data, api_key=None) -> bool` — writes arbitrary fields to a bill item (sync/scheduler use only)
  - `update_bill_gov_url(webflow_id, new_url, api_key=None) -> bool` — thin wrapper over `update_bill_fields()` for the `gov-url` field
- **`OrgPosition`** — `name, org_type, slug, position` (`position` is the string `"support"`/`"oppose"`, not a bool)
- **`BillOrgPositionsResult`** — `bill_name, supporting_orgs, opposing_orgs, found`
- **`BillPosition`** — `name, bill_id, slug, position`
- **`OrgBillPositionsResult`** — `org_name, supported_bills, opposed_bills, found`
- **`BillDetailsResult`** — `name, identifier, status, description, jurisdiction, slug, session, bill_prefix, bill_number, openstates_url, found`
- **`LegislatorDetailsResult`** — `name, party, chamber, district, jurisdiction, score, slug, openstates_id, found` (field is `score`, not `ddp_score`; no `webflow_id` field — use `slug`)
- **`OrgDetailsResult`** — `name, org_type, website, description, slug, found`
- **Module-level formatters** (imported by `core/agent.py` for context injection, labeled "Authoritative Source — Webflow CMS"): `format_org_positions_context(BillOrgPositionsResult)`, `format_org_bill_positions_context(OrgBillPositionsResult)`, `format_bill_verification_context(BillDetailsResult)`, `format_legislator_verification_context(LegislatorDetailsResult)`, `format_org_verification_context(OrgDetailsResult)` — all return `""` when `result.found` is `False`

## Broker lookups and links to our pages (`services/broker_lookup.py`, `utils/ddp_urls.py`, VOTEBOT-15)

Canonical-id index only; the Webflow versions above stay for the legacy index and are deleted with it.

- **`BrokerLookupService`** — ddp-broker-py public reads, every method returns None on any problem (no root, unreachable, non-object body, `found: false`): `get_bill_org_positions(jurisdiction, session, gov_id) -> BillOrgPositions | None`, `get_org_bill_positions(org_id) -> list[BillPositionOfOrg] | None` (follows `next`, at most 3 pages of 200), `get_org_details(org_id) -> OrgDetails | None`. Formatters: `format_bill_org_positions`, `format_org_bill_positions(org, positions, site_base_url)`, `format_org_details` (labelled "Authoritative Source: Digital Democracy Project database").
- **Agent branches** — `_prefetch_bill_org_positions`, `_prefetch_org_bill_positions` and `_verify_from_webflow` call the broker when `settings.bill_filter_key == "ocd_bill_id"`; on dispute only organizations are verified (bills go through the OpenStates vote verification, the broker has no public legislator profile). `_broker_org_id(page_context)` = an all-digit `page_context.id`.
- **`ddp_bill_url(base, jurisdiction, session, gov_id)`**, **`ddp_bill_url_from_metadata(base, metadata)`** — `{base}/explore/{JURISDICTION}/{SESSION}/{IDENTIFIER}` (identifier URL-encoded). Nothing is built without `DDP_SITE_BASE_URL`.
- **`RetrievalService._link_to_ddp_pages(chunks)`** — last step of `retrieve()`: on the canonical index with the base URL set, bill chunks (`bill-text`, `bill-version-diff`, `bill-votes`) get `metadata["url"]` = our page and keep the original as `source_url`, so the source header the model cites, the citation match and the citation link agree. Do the mapping here, not in the prompt builder or the citation code.

## Button cache (`services/button_cache.py`)

- **Cache id (VOTEBOT-15)** — `VoteBotAgent._button_cache_scope(page_context) -> (id, version) | None` decides what the `slug` argument below means. Legacy index: the bill slug, no version. Canonical-id index: the `ocd_bill_id`, plus the bill's **current version `document_id`** (looked up through `BillVersionService`, like retrieval) stored in the entry as `document_id` and compared on every hit, so a new version is a miss and nothing has to publish an invalidation for that index. Unknown current version (or a lookup that raises) → not cached, not served. The 120 s in-process version cache applies, as it does to retrieval, so a new version can take up to that long to be noticed. Pages without the id (canonical) or the slug (legacy) are not cached.
- **`ButtonCache`** — Redis-backed 7-day-safety-TTL cache for Summary and Pros & Cons button responses. Auto-invalidated via `votebot:cache:invalidate` pub/sub from DDP-Sync on bill version change. Takes a `RedisStore` in its constructor (`ButtonCache(redis_store)`) — get the shared instance via `get_button_cache()`, not by constructing directly.
  - `get(slug, button_type) -> dict | None` — reads v2 key first, falls back to a read-only v1 dual-read during the migration window
  - `set(slug, button_type, response: dict) -> None`
  - `invalidate_bill(slug) -> int` — clears all cacheable buttons for a slug, returns count deleted
  - `list_cached_keys() -> list[str]` — SCAN-based listing for startup reconciliation
- **Module-level functions (not `ButtonCache` methods)**:
  - `get_button_cache() -> ButtonCache` — lazy singleton accessor
  - `start_invalidate_subscriber(redis_store)` — starts the pub/sub listener at app startup, with auto-reconnect/backoff supervisor loop
  - `stop_invalidate_subscriber()` — cancels the subscriber task at shutdown
  - `reconcile_on_startup(redis_store)` — drops cache entries older than each bill's `last_checked` (`ddp:bill_version:*`), covering invalidation events missed while VoteBot was down
  - `make_key(slug, button_type)` / `make_v1_key(slug, button_type)` — v2/legacy-v1 Redis key builders
- Key/constant reference: `CACHEABLE_TYPES = ("summary", "pros_cons")`, `ALL_BUTTON_TYPES = ("summary", "pros_cons", "status_votes")`, `KEY_PREFIX = "votebot:button:v2:"`, `LEGACY_KEY_PREFIX_V1 = "votebot:button:"`, `SAFETY_TTL` = 7 days, `INVALIDATE_CHANNEL = "votebot:cache:invalidate"`
- Button types: `"summary"`, `"pros_cons"` (cached). `"status_votes"` is **never cached** — always hits live OpenStates.
- Admin endpoint: `DELETE /votebot/v1/cache/button/{slug}` (`api/routes/cache_admin.py`) — returns `{slug, deleted: int}`

## Bill info tool (`services/bill_votes.py`)

- **`BillVotesService`** — live OpenStates lookup, used when RAG confidence is low or query is about a bill not in the system:
  - `get_bill_info(jurisdiction, session, bill_identifier) -> BillInfoResult | None` — full bill data, with same-year fallback for state sessions. **This is the primary method** (drives the `get_bill_info` LLM tool)
  - `get_bill_votes(jurisdiction, session, bill_identifier) -> BillVotesResult | None` — votes only, no full bill metadata. (There is no `get_votes` method — use this name.)
  - `get_bill_info_by_url(openstates_url) -> BillInfoResult | None` / `get_bill_votes_by_url(openstates_url) -> BillVotesResult | None` — preferred when the CMS already stores the canonical OpenStates URL (bypasses session/identifier inference, needed for special sessions like FL "2026D")
  - `format_bill_info_document(BillInfoResult) -> str` — formats full bill info (incl. per-party vote breakdown) as markdown for LLM context
  - `find_legislator_in_votes(legislator_name, votes, bill_identifier) -> dict | None` — searches a list of `BillVote` for a legislator's vote, prioritizing final-passage votes over procedural ones
  - `lookup_legislator_vote(legislator_name, jurisdiction, session, bill_identifier) -> dict | None` — `get_bill_info()` + `find_legislator_in_votes()` combined
- **`BillInfoResult`** — `bill_id, bill_identifier, jurisdiction, title, description, session, status, chamber, sponsors, actions, votes, openstates_url, found`
- **`BillVotesResult`** — `bill_id, bill_identifier, jurisdiction, title, votes: list[BillVote], openstates_id`. No `total_yes`/`total_no`/`total_other` fields — aggregate counts live on `BillVote`, not here.
- **`BillVote`** — `vote_id, motion_text, result, date, chamber, yes_count, no_count, other_count, votes: list[VoteRecord]` (field is `motion_text`, not `motion`; field is `votes`, not `individual_votes`)
- **`VoteRecord`** — `legislator_id, legislator_name, vote, party` (field is `vote`, not `vote_option`; field is `legislator_id`, not `person_id`)

## Query logger (`services/query_logger.py`)

- **`QueryLogger`** — async JSONL logger, date-partitioned files (e.g. `logs/queries/2026-02-08.jsonl`), `aiofiles` + `O_APPEND` for atomic multi-worker writes.
  - `log_event(*, event_type, session_id, visitor_id=None, message=None, response=None, primary_intent=None, sub_intent=None, confidence=None, retrieval_count=None, grounding_status=None, web_search_used=False, bill_votes_tool_used=False, button_type=None, cache_hit=None, turn_count=None, terminal_state=None, page_context=None, ...) -> None` — **single unified method**, not three separate `log_message_received`/`log_query_processed`/`log_conversation_ended` methods. `event_type` (a free-form string set by the caller, e.g. `"message_received"`/`"query_processed"`/`"conversation_ended"`) distinguishes the event kind within one shared JSONL schema/file.
  - `log_query(*, session_id, message, response, confidence, citations, page_context, channel, duration_ms, ...)` — legacy-format wrapper kept for backward compatibility; new code should use `log_event()`.
- `get_query_logger() -> QueryLogger | None` — module-level singleton accessor; returns `None` when `settings.query_log_enabled` is `False`
- **Do not log PII** — scrub tokens, credentials, and personal data before logging

## Redis store (`services/redis_store.py`)

Singleton: `get_redis_store() -> RedisStore`. All methods no-op gracefully when Redis is down (`connect()`/`disconnect()` called from `main.py` lifespan).

- **Thread/session mapping** — `set_thread_mapping(thread_ts, session_id)` / `get_session_for_thread(thread_ts)` / `remove_thread_mapping(thread_ts)` — Slack human handoff cross-worker state. (Method names are `*_thread_mapping`/`get_session_for_thread`, not `set_thread_session`/`get_thread_session`.)
- **Button cache** — delegated to `ButtonCache`; don't interact with button Redis keys directly
- **Active jurisdictions** — `add_active_jurisdiction(code)` / `get_active_jurisdictions()`
- **Bill version cache** — `set_bill_version(webflow_id, version_data)` / `get_bill_version(webflow_id) -> dict | None`. Both live in this class; the sync/scheduler side (`scripts/sync_bills.py` etc., outside `src/votebot/`) writes via `set_bill_version`, VoteBot's agent/retrieval path only reads via `get_bill_version`.
- **Sync task/checkpoint storage** (used by scheduler/sync scripts, not the chat agent) — `set_sync_task`/`get_sync_task`, `add_sync_checkpoint`/`get_sync_checkpoints`/`copy_sync_checkpoints`
- **Scheduler leader election** (used by scheduler/sync scripts) — `acquire_scheduler_lock(worker_id)` / `refresh_scheduler_lock(worker_id)` / `release_scheduler_lock(worker_id)`, backed by `SCHEDULER_LOCK_KEY` with a 5-minute TTL
- **Pub/sub** — `publish_agent_event(event_type, session_id, payload)` / `subscribe_agent_events(handler)` on `"votebot:agent_events"` (Slack human handoff cross-worker); button cache invalidation (`"votebot:cache:invalidate"`) is handled separately in `button_cache.py`, not through this pub/sub pair

## Embeddings service (`services/embeddings.py`)

- **`EmbeddingService`** — OpenAI `text-embedding-3-large` (`EMBEDDING_DIMENSION = 3072`). Convenience methods: `embed_documents(texts) -> list[list[float]]`, `embed_query(text) -> list[float]` (wrap the lower-level `embed(text) -> EmbeddingResult` / `embed_batch(texts, batch_size=100) -> list[EmbeddingResult]`). `get_dimension()` classmethod returns `EMBEDDING_DIMENSION`.
- **`EmbeddingResult`** — `embedding, tokens_used, model`
- **`EmbeddingServiceFactory.get_instance()`** — singleton

## Web search service (`services/web_search.py`)

- **`WebSearchService`** — Tavily fallback when RAG confidence < threshold. **Called directly by `core/agent.py`** (`_perform_web_search` → `self.web_search.search()` / `search_legislation()` / `search_legislator()`) — this is a *separate* mechanism from `LLMService`'s built-in OpenAI Responses API `web_search_preview` tool (the `enable_web_search` flag on `LLMService.stream()`/`complete()`). Don't conflate the two web-search paths.
  - `search(query, num_results=5, search_depth="basic", include_domains=None, exclude_domains=None) -> list[WebSearchResult]`
  - `search_legislation(query, num_results=5)` / `search_legislator(query, num_results=5)` / `search_news(query, num_results=5)` — domain-scoped convenience wrappers over `search()`
  - `format_results_for_context(results, max_length=2000) -> str`
- **`WebSearchResult`** — `title, url, snippet, source, score=0.0` (field is `snippet`, not `content`; also has a `source` field)

## Slack service (`services/slack.py`)

- **`SlackService`** — human handoff via Slack Socket Mode. Get the shared instance via `get_slack_service()`. thread↔session mapping is NOT managed by this class — that's `RedisStore.set_thread_mapping`/`get_session_for_thread` (see Redis store above). Actual methods (names differ from a generic `initiate_handoff`/`send_agent_message` pair):
  - `start(on_agent_message, on_handoff_resolved)` / `stop()` — connect/disconnect the Socket Mode client
  - `create_handoff_thread(session_id, page_context, latest_message, conversation_history) -> str | None` — creates the handoff thread in the support channel, returns `thread_ts`
  - `relay_user_message(thread_ts, message) -> bool` — relay a visitor message into an existing thread
  - `send_handoff_resolved_message(thread_ts) -> bool`
- **Pause/resume contract**: user says "talk to human" → `requires_human=True` in response → WebSocket calls `SlackService.create_handoff_thread()` → agent replies in Slack thread (`on_agent_message` callback) → pub/sub (`RedisStore.publish_agent_event`) delivers to correct worker → `✅`/`heavy_check_mark` reaction (`on_handoff_resolved` callback) closes the thread.

## Federal legislator cache (`utils/federal_legislator_cache.py`)

- **`FederalLegislatorCache`** — in-memory cache of US Congress members. `lookup_with_info(name) -> dict | None` — returns `{person_id, name, party, state}`. Used to resolve federal voter names to stable OpenStates person IDs in vote records and legislator follow-up queries. `_get_federal_cache()` in `retrieval.py` is the lazy module-level accessor.

## Legislative calendar (`utils/legislative_calendar.py`)

- **`StateLegislativeCalendar`** — `is_in_session(state_code, check_date=None) -> bool`. Same class as in ddp-sync. **Currently unused/orphaned in `src/votebot/`** — not imported by `retrieval.py` or anywhere else outside this file, despite the "used in retrieval" framing implied here previously. Confirm with a fresh grep before relying on it being wired in.

## Misc utils (`utils/logging.py`, `utils/metrics.py`)

- **`utils/logging.py`** — `setup_logging(log_level)` (called from `main.py` at startup), `get_logger(name=None)`, `RequestLogger` context manager, `log_performance(logger, operation, duration_ms, **extra)`.
- **`utils/metrics.py`** — `MetricsCollector` (`record`, `increment`, `timer`, `get_summary`, `get_report`), `get_metrics()` singleton, convenience functions `record_latency`/`record_tokens`/`increment_request_count`/`increment_error_count`. **Currently unused/dead code** — nothing outside this file calls `get_metrics()` or the convenience functions; `structlog` + `QueryLogger` are the metrics path actually in use.

## API schemas (`api/schemas/chat.py`)

- **`PageContext`** — `type: "bill"|"legislator"|"organization"|"general"`, `id, jurisdiction, session, title, url, slug, webflow_id, ocd_bill_id`. `ocd_bill_id` is the bare OpenStates UUID (no `ocd-bill/` prefix, matching the vectors' metadata). The filter source for retrieval — always pass through rather than building filters from raw message text. (Note the `session` field — legislative session, e.g. `"2025"`/`"119"` — is easy to miss.)
- **`NavigationContext`** — `previous_pages: list[str], time_on_page, scroll_depth` — optional navigation signal for intent disambiguation
- **`ClientMetadata`** — `client_id, client_version, user_agent, platform, entry_referrer, page_url` — optional client/analytics metadata
- **`ChatRequest`** — `message, session_id, human_active, page_context, navigation_context, client_metadata, conversation_history, button`
- **`ChatResponse`** — `response, citations, confidence, requires_human, suppressed, web_search_used, web_citations, bill_votes_tool_used, metadata, timestamp`
- **`StreamChunk`** (schema) ⚠️ — `chunk, done, citations, metadata`. Wire format for the REST **SSE** streaming endpoint (`api/routes/chat.py::chat_stream`, field name is `chunk`, not `text`) — the WebSocket route (`api/routes/websocket.py`) does NOT use this model; it sends raw `{"type": "stream_chunk", "payload": {"text": ...}}` dicts instead. **Name collision with `services/llm.py::StreamChunk`** (the internal LLM token, fields `text`/`done`/`web_search_used`). Same name, different modules, different shapes/wire formats. Always check the import path.
- **`Citation`** — `source, document_id, excerpt, url, relevance_score`
- **`WebCitation`** ⚠️ — `url, title, snippet`. **Near-duplicate of `services/llm.py::WebSearchCitation`** (same fields, dataclass vs Pydantic). Don't add a third web citation shape.
- **`ResponseMetadata`** — `model, tokens_used, retrieval_count, latency_ms, cached`

## Pinecone document types (controlled vocabulary)

Same index and namespace as DDP-Sync (`votebot-large` today; `ddp-knowledge-base`, keyed by `ocd_bill_id`, once `PINECONE_INDEX_NAME` points at it — PLAN-enterprise-search.md 5.6). VoteBot is **read-only** — it never writes to Pinecone directly; all writes go through DDP-Sync. Vectors on the canonical-id index carry `ocd_bill_id` (bare UUID), `jurisdiction` (upper-case code), `session_code`, `gov_id`, `url`/`source_url` and, for versions, `document_id`, `version_note`, `version_date`, `version_stage`, `version_ordinal`; they carry **no** `slug` or `webflow_id`.

| `document_type` | Retrieved by | Notes |
|---|---|---|
| `bill` | Phase 2 (summary), Phase 4a (org) | CMS summary chunks |
| `bill-text` | Phase 1 | Legacy index: current legislative text, overwritten each version by DDP-Sync. Canonical-id index: **one document per version** (`bill-text:{ocd_bill_id}:{document_id}`), filtered to the current version by default |
| `bill-text-history` | **Never retrieved by VoteBot** | Permanent historical text; stored for future use |
| `bill-changelog` | Phase 5 (changelog intent only) | LLM-generated diffs; requires `webflow_id` filter |
| `bill-version-diff` | Phase 5 (changelog intent only; canonical-id index) | api-v3's stored diff against the previous version, embedded verbatim, labelled with `from_version_note`/`from_version_date`/`from_document_id`; replaces `bill-changelog`. None for `unknown`-stage versions |
| `bill-votes` | Phase 4b | Vote records per bill |
| `legislator` | Standard retrieval | Legislator profiles |
| `legislator-votes` | Phase 4b | Reverse index: per-legislator voting history |
| `organization` | Phase 4a, org retrieval | Org profiles with bill positions |
| `training` | General retrieval | Behaviour customisation docs |

## Bill versions (`services/bill_versions.py`, VOTEBOT-10)

- **`BillVersionService.get_versions(ocd_bill_id) -> list[BillVersion] | None`** — asks api-v3 for the bill's ordered versions (`/bills/ocd-bill/{uuid}?include=versions`, through `openstates_base_url`/`openstates_headers`), cached 120 s in-process; None when unavailable (remembered 45 s so a down api-v3 costs one 3 s wait, not one per message) and always None unless `use_ddp_openstates_replica` is on, because the public OpenStates API has none of the DDP version fields.
- **`BillVersion`** — `document_id` (api-v3's `archived_document_id` as a string, the vectors' `document_id`; None if not archived), `note, date, stage, ordinal`.
- **`current_version(versions) -> BillVersion | None`** — the latest **classifiable** version (stage not `unknown`), and only if it is archived; if the latest classifiable one has no archived document yet the answer is None, never an older version. api-v3's order (latest last) is trusted, never re-sorted. **Do not store an is_current flag on vectors and do not recompute version order or diffs here**: ordering is `api/version_ordering.py`, diffs are api-v3's stored `diff_from_previous_version`.

## Content resolution route (`api/routes/content.py`)

`GET /content/resolve?url=` turns a DDP URL into the page context the widget sends back on every message. Two paths, chosen by URL shape: ddp-next bill URLs (`/bills/{broker_id}`, `/bills/{jurisdiction}/{session}/{gov_id}`) go through ddp-broker-py (`_match_ddp_next_bill`, `resolve_ddp_next_bill`, `_broker_get`) and return `ocd_bill_id`; everything else is the Webflow CMS path (`fetch_webflow_item_by_slug`) returning `webflow_id`. **ddp-broker-py has no bill-detail endpoint** — use its `/api/bills/{id}/scorecard/` and `/api/bills/resolve/`. The websocket route builds `PageContext` from the payload in one place (`_page_context_from_payload`); a new page-context key must be added there or it is silently dropped.

## Feature flags (config.py `Settings`)

| Flag | Default | Controls |
|---|---|---|
| `bill_votes_tool_enabled` | `true` | `get_bill_info` LLM tool |
| `bill_votes_rag_confidence_threshold` | `0.4` | Confidence below which tool fires |
| `webflow_org_lookup_enabled` | `true` | Runtime CMS org position fetch |
| `web_search_enabled` | `true` | Tavily / Responses API web search (master switch, both mechanisms) |
| `web_search_context_size` | `"medium"` | OpenAI `web_search_preview` tool's `search_context_size` |
| `web_search_on_low_confidence` | `true` | Whether `complete_with_fallback()` auto-enables web search at all |
| `web_search_confidence_threshold` | `0.5` | Confidence below which web search fires (general/bill pages) |
| `web_search_legislator_confidence_threshold` | `0.7` | Same, for legislator pages (fires more easily) |
| `web_search_organization_confidence_threshold` | `0.7` | Same, for organization pages (fires more easily) |
| `quick_action_buttons_enabled` | `false` | Summary/Pros&Cons/Status buttons + Redis cache |
| `enhanced_citation_prompt` | `false` | Stricter citation instruction variant |
| `query_log_enabled` | `true` | JSONL event logging (`QueryLogger.log_event`) |
| `pinecone_index_name` | `"votebot-large"` | Which index is read **and**, via `bill_filter_key`, how a bill is pinned in filters (`votebot-large` → `webflow_id`, anything else → `ocd_bill_id`). Rollback is this one value |
| `ddp_broker_api_root` | `""` | ddp-broker-py base URL for `/content/resolve` on ddp-next URLs; empty → 503 for those URLs only |

---

## Discipline checklist for every new PLAN

Before sketching a new dataclass / retrieval phase / helper / prompt section:

1. **Grep first.** `grep -rn "class <Name>\|def <name>" src/votebot/` and check this catalog.
2. **Check retrieval phases.** Phases 1–5 cover text, summary, orgs, votes, and changelogs. New retrieval logic extends `_retrieve_bill_with_text_priority()` — don't add raw `vector_store.query()` calls in the agent.
3. **Check result types.** `AgentResult`, `StreamChunkData`, `RetrievalResult`, `LLMResponse` cover most return shapes. Don't add per-feature result types.
4. **Check the Webflow lookup service.** `WebflowLookupService` is the one CMS read primitive. Don't add httpx calls to Webflow in the agent or retrieval.
5. **Check intent taxonomy.** `SubIntent` and `CHANGELOG_KEYWORDS` are the canonical intent vocabulary. New keyword lists belong in `intent.py`, not inline in retrieval.
6. **Check `format_retrieved_chunks`.** Special chunk headers (like the `bill-changelog` version transition label) belong here, not in the agent or retrieval.
7. **Check `build_system_prompt`.** Prompt additions go into named constants in `prompts.py`, then referenced from `build_system_prompt`. Don't concatenate prompt strings in the agent.
8. **Check `VALID_RETRIEVAL_SOURCES`.** Any new `document_type` used in retrieval must be added here, or `normalize_retrieval_sources` will log it as unknown in analytics.
