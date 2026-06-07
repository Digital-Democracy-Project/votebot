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
    - Phase 5: changelog (`bill-changelog` + webflow_id, **only on changelog intent**)
  - `_retrieve_organization_priority(query, filters, max_chunks) -> list[SearchResult]`
  - `_build_filters(page_context, query) -> dict` — builds Pinecone filter from page context; use this, never build filters inline
  - `_deduplicate(results) -> list[SearchResult]`
  - `retrieve_for_bill(query, bill_id, jurisdiction) -> RetrievalResult`
  - `retrieve_for_legislator(query, legislator_id, jurisdiction) -> RetrievalResult`
  - `retrieve_general(query, jurisdiction) -> RetrievalResult`
- **`RetrievalResult`** — `chunks, query_used, filters_applied, total_retrieved`
- **`RetrievalConfig`** — `max_chunks, similarity_threshold, use_hybrid_search, deduplicate`
- **`ExtractedBillInfo`** — `bill_prefix, bill_number, jurisdiction`. Properties: `bill_id`, `slug_pattern`
- **`HybridRetrievalService`** — subclass of `RetrievalService`; keyword search stub, not yet implemented

**Retrieval isolation rule**: `bill-text-history` and `bill-changelog` are invisible to all existing phases by design (explicit `document_type` filters). Only Phase 5 queries `bill-changelog`, and only on changelog intent. Never add unfiltered fallback queries that could surface these types in normal responses.

## Intent classification (`utils/intent.py`)

Single source of truth for intent taxonomy and retrieval vocabulary.

- **`PrimaryIntent`** StrEnum — `BILL, LEGISLATOR, ORGANIZATION, GENERAL, OUT_OF_SCOPE`
- **`SubIntent`** StrEnum — `SUMMARY, SUPPORT_OPPOSITION, VOTE_HISTORY, STATUS, EXPLANATION, COMPARISON, CHANGELOG, VOTING_RECORD, CONTACT, BIO, DDP_SCORE, SPONSORED_BILLS, POSITIONS, INFO, BILL_ALIGNMENT, NAVIGATION, HOW_TO_VOTE, ABOUT_DDP, ISSUE_AREA, TEXT_EDITING, CIVIC_ACTION, GREETING, OFF_TOPIC, META, UNKNOWN`
- **`CHANGELOG_KEYWORDS: list[str]`** — canonical keyword list for changelog intent detection. **Single source of truth** — imported by `retrieval.py` for Phase 5 detection. `retrieval.py` extends it with `["amendment", "amended"]` for broader retrieval recall without polluting analytics.
- **`VALID_RETRIEVAL_SOURCES: frozenset`** — controlled vocabulary for `document_type` values. Add new document types here AND in ddp-sync's document type table. Values: `bill, bill-text, bill-history, bill-votes, bill-changelog, bill-text-history, legislator, legislator-votes, organization, training`
- **`classify_primary_intent(page_type, message) -> str`**
- **`classify_sub_intent(primary_intent, message) -> str`**
- **`normalize_retrieval_sources(raw_sources) -> list[str]`** — maps unknown types to `"unknown"` with a warning; don't skip this

## LLM service (`services/llm.py`)

- **`LLMService`** — OpenAI Responses API + Chat Completions. Methods:
  - `complete(messages, system_prompt, tools, enable_web_search, ...) -> LLMResponse` — non-streaming; uses `_join_response_blocks()` for block-boundary whitespace fix
  - `stream(messages, system_prompt, enable_web_search, ...) -> AsyncIterator[StreamChunk]` — routes to Responses API (web search on) or Chat Completions (web search off)
  - `health_check() -> bool`
- **`_join_response_blocks(response) -> str`** — module-level helper. Replaces `response.output_text` to fix SDK block-boundary whitespace loss (`"".join()` drops `\n\n` at block boundaries). **Do not use `response.output_text` directly.**
- **`LLMResponse`** — `content, tokens_used, model, finish_reason, web_search_used, web_citations, bill_votes_tool_used, response_id`
- **`StreamChunk`** ⚠️ — `text, done, web_search_used`. **Name collision**: `api/schemas/chat.py` also has a class called `StreamChunk` (the WebSocket wire format). When importing, always verify which module you're pulling from. Consider renaming one of them in a future cleanup.
- **`WebSearchCitation`** ⚠️ — `url, title, snippet`. **Near-duplicate**: `api/schemas/chat.py` has `WebCitation` with the same three fields as a Pydantic model. One is a dataclass (internal), one is the API schema (external). There is a translation step between them. Don't add a third web citation shape.
- **`BillVotesToolResult`** — tool call result shape from `get_bill_info`
- **`LLMServiceFactory.get_instance()`** — singleton

## Agent (`core/agent.py`)

- **`VoteBotAgent`** — orchestrates retrieval → augmentation → LLM → verification. Two entry points:
  - `process_message(message, session_id, page_context, conversation_history, button) -> AgentResult` — non-streaming (HTTP endpoint)
  - `process_message_stream(message, session_id, page_context, conversation_history, button) -> AsyncIterator[StreamChunkData]` — streaming (WebSocket)
  - Note: `process_message_stream` checks the button cache first and yields the cached response without calling LLM if it's a cache hit. No `llm.stream()` call occurs on cache hits.
- **`AgentResult`** — `response, citations, confidence, requires_human, tokens_used, retrieval_count, web_search_used, bill_votes_tool_used, metadata`
- **`StreamChunkData`** — `text, done, citations, confidence, requires_human, metadata, web_search_used, bill_votes_tool_used`

## Prompts (`core/prompts.py`)

- **`SYSTEM_PROMPT_BASE`** — base system prompt. Contains the bullet-per-line instruction. **Do not duplicate formatting rules inline.**
- **`BILL_CONTEXT_PROMPT`** — bill page context; includes changelog guidance ("cite version transition explicitly; say so if no changelog available")
- **`LEGISLATOR_CONTEXT_PROMPT`**, **`ORGANIZATION_CONTEXT_PROMPT`**, **`GENERAL_CONTEXT_PROMPT`** — context-specific prompt sections
- **`RAG_CONTEXT_TEMPLATE`** — wrapper for retrieved context injected into the prompt
- **`CITATION_INSTRUCTION`** / **`ENHANCED_CITATION_INSTRUCTION`** — citation formatting rules; `ENHANCED_CITATION_INSTRUCTION` gated on `settings.enhanced_citation_prompt`
- **`build_system_prompt(page_type, page_info, include_rag_context, retrieved_context) -> str`** — assembles the full system prompt; always use this, never concatenate prompts manually
- **`format_retrieved_chunks(chunks: list[dict]) -> str`** — formats retrieved chunks for RAG injection. Adds `**Version Change:** from → to` header for `bill-changelog` chunks. **Use this, don't write inline formatters.**
- **`_build_ddp_url(metadata, doc_type) -> str | None`** — builds DDP citation URL from slug in metadata

## Webflow runtime lookup (`services/webflow_lookup.py`)

Bidirectional CMS fetch used at query time. **Read-only at runtime** — writes go through DDP-Sync.

- **`WebflowLookupService`** — methods:
  - `get_bill_org_positions(bill_webflow_id, bill_slug) -> BillOrgPositionsResult` — org positions for a bill
  - `get_org_bill_positions(org_webflow_id) -> OrgBillPositionsResult` — bills for an org
  - `get_bill_details(slug) -> BillDetailsResult` — bill metadata for dispute verification
  - `get_legislator_details(slug) -> LegislatorDetailsResult` — resolves slug → OpenStates ID + details
  - `get_org_details(webflow_id) -> OrgDetailsResult`
- **`OrgPosition`** — `org_name, org_type, org_slug, supports`
- **`BillOrgPositionsResult`** — `found, supporting_orgs, opposing_orgs, bill_title`
- **`BillPosition`** — `bill_name, bill_slug, supports`
- **`OrgBillPositionsResult`** — `found, bills_supported, bills_opposed, org_name`
- **`BillDetailsResult`** — `found, title, identifier, status, description, jurisdiction`
- **`LegislatorDetailsResult`** — `found, name, party, chamber, district, ddp_score, openstates_id, webflow_id`
- **`OrgDetailsResult`** — `found, name, org_type, website, description`

## Button cache (`services/button_cache.py`)

- **`ButtonCache`** — Redis-backed 7-day cache for Summary and Pros & Cons button responses. Auto-invalidated via `votebot:cache:invalidate` pub/sub from DDP-Sync on bill version change.
  - `get(slug, button_type) -> dict | None`
  - `set(slug, button_type, response_data)`
  - `invalidate_bill(slug)` — clears all buttons for a slug
  - `start_invalidate_subscriber()` — starts pub/sub listener at app startup
- Button types: `"summary"`, `"pros_cons"` (cached). `"status_votes"` is **never cached** — always hits live OpenStates.
- Admin endpoint: `DELETE /votebot/v1/cache/button/{slug}` — returns `{slug, deleted: int}`

## Bill info tool (`services/bill_votes.py`)

- **`BillVotesService`** — live OpenStates lookup, used when RAG confidence is low or query is about a bill not in the system:
  - `get_bill_info(jurisdiction, session, bill_identifier) -> BillInfoResult`
  - `get_votes(bill_id, jurisdiction, session) -> BillVotesResult`
- **`BillInfoResult`** — full bill data: title, sponsors, status, actions, votes, OpenStates URL
- **`BillVotesResult`** — `votes: list[BillVote], total_yes, total_no, total_other`
- **`BillVote`** — `chamber, motion, result, date, yes_count, no_count, other_count, individual_votes`
- **`VoteRecord`** — `legislator_name, party, vote_option, person_id`

## Query logger (`services/query_logger.py`)

- **`QueryLogger`** — event-based JSONL logging. Three event types (all go to date-partitioned files):
  - `log_message_received(session_id, visitor_id, message, page_context, ...)` 
  - `log_query_processed(session_id, ..., intent, retrieval_count, grounding_status, web_search_used, ...)`
  - `log_conversation_ended(session_id, ..., turn_count, terminal_state, ...)`
- **Do not log PII** — scrub tokens, credentials, and personal data before logging

## Redis store (`services/redis_store.py`)

Singleton: `get_redis_store() -> RedisStore`. All methods no-op gracefully when Redis is down.

- **Thread/session mapping** — `set_thread_session(thread_ts, session_id)` / `get_thread_session(thread_ts)` — Slack human handoff cross-worker state
- **Button cache** — delegated to `ButtonCache`; don't interact with button Redis keys directly
- **Active jurisdictions** — `add_active_jurisdiction(code)` / `get_active_jurisdictions()`
- **Bill version cache** (read-only at VoteBot side) — `get_bill_version(webflow_id) -> dict | None`. Written by DDP-Sync; VoteBot reads `bill_slug` for startup reconciliation and `chunk_count` is DDP-Sync internal
- **Pub/sub** — subscribe to `"votebot:cache:invalidate"` (button cache invalidation from DDP-Sync), publish to `"votebot:agent_events"` (Slack human handoff cross-worker)

## Embeddings service (`services/embeddings.py`)

- **`EmbeddingService`** — OpenAI `text-embedding-3-large`. Methods: `embed_documents(texts)`, `embed_query(text)`
- **`EmbeddingResult`** — `embedding, tokens_used, model`
- **`EmbeddingServiceFactory.get_instance()`** — singleton

## Web search service (`services/web_search.py`)

- **`WebSearchService`** — Tavily fallback when RAG confidence < threshold. Not called directly — wired into `LLMService` via the `enable_web_search` flag on `stream()` / `complete()`
- **`WebSearchResult`** — `url, title, content, score`

## Slack service (`services/slack.py`)

- **`SlackService`** — human handoff via Slack Socket Mode. Manages `thread_to_session` mapping (local dict + Redis for cross-worker). Methods: `initiate_handoff(session_id, message, page_context, history)`, `send_agent_message(session_id, message)`.
- **Pause/resume contract**: user says "talk to human" → `requires_human=True` in response → WebSocket calls `SlackService.initiate_handoff()` → agent replies in Slack thread → pub/sub delivers to correct worker → `✅` reaction closes the thread.

## Federal legislator cache (`utils/federal_legislator_cache.py`)

- **`FederalLegislatorCache`** — in-memory cache of US Congress members. `lookup_with_info(name) -> dict | None` — returns `{person_id, name, party, state}`. Used to resolve federal voter names to stable OpenStates person IDs in vote records and legislator follow-up queries. `_get_federal_cache()` in `retrieval.py` is the lazy module-level accessor.

## Legislative calendar (`utils/legislative_calendar.py`)

- **`StateLegislativeCalendar`** — `is_in_session(state_code) -> bool`. Same class as in ddp-sync; used in retrieval to decide whether to surface jurisdiction-specific content.

## API schemas (`api/schemas/chat.py`)

- **`PageContext`** — `type: "bill"|"legislator"|"organization"|"general"`, `id, slug, webflow_id, title, jurisdiction, url`. The filter source for retrieval — always pass through rather than building filters from raw message text.
- **`ChatRequest`** — `message, session_id, human_active, page_context, conversation_history, button`
- **`ChatResponse`** — `response, citations, confidence, requires_human, web_search_used, bill_votes_tool_used, metadata, suppressed`
- **`StreamChunk`** (schema) ⚠️ — WebSocket wire format. **Name collision with `services/llm.py::StreamChunk`** (the internal LLM token). Same name, different modules, different shapes. Always check the import path.
- **`Citation`** — `source, document_id, excerpt, url, relevance_score`
- **`WebCitation`** ⚠️ — `url, title, snippet`. **Near-duplicate of `services/llm.py::WebSearchCitation`** (same fields, dataclass vs Pydantic). Don't add a third web citation shape.
- **`ResponseMetadata`** — `model, tokens_used, retrieval_count, latency_ms, cached`

## Pinecone document types (controlled vocabulary)

Same index (`votebot-large`) and namespace as DDP-Sync. VoteBot is **read-only** — it never writes to Pinecone directly; all writes go through DDP-Sync.

| `document_type` | Retrieved by | Notes |
|---|---|---|
| `bill` | Phase 2 (summary), Phase 4a (org) | CMS summary chunks |
| `bill-text` | Phase 1 | Current legislative text; overwritten each version by DDP-Sync |
| `bill-text-history` | **Never retrieved by VoteBot** | Permanent historical text; stored for future use |
| `bill-changelog` | Phase 5 (changelog intent only) | LLM-generated diffs; requires `webflow_id` filter |
| `bill-votes` | Phase 4b | Vote records per bill |
| `legislator` | Standard retrieval | Legislator profiles |
| `legislator-votes` | Phase 4b | Reverse index: per-legislator voting history |
| `organization` | Phase 4a, org retrieval | Org profiles with bill positions |
| `training` | General retrieval | Behaviour customisation docs |

## Feature flags (config.py `Settings`)

| Flag | Default | Controls |
|---|---|---|
| `bill_votes_tool_enabled` | `true` | `get_bill_info` LLM tool |
| `bill_votes_rag_confidence_threshold` | `0.4` | Confidence below which tool fires |
| `webflow_org_lookup_enabled` | `true` | Runtime CMS org position fetch |
| `web_search_enabled` | `true` | Tavily / Responses API web search |
| `web_search_confidence_threshold` | `0.5` | Confidence below which web search fires |
| `quick_action_buttons_enabled` | `false` | Summary/Pros&Cons/Status buttons + Redis cache |
| `enhanced_citation_prompt` | varies | Stricter citation instruction variant |
| `query_log_enabled` | `true` | JSONL event logging |

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
