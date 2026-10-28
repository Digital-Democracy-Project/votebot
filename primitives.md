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

- **`VectorStoreService`** — Pinecone client. Lazy-initialized. **The Pinecone client is synchronous**: every call to it from async code goes through `asyncio.to_thread` (`query` and `health_check` do; VOTEBOT-36), because on the event loop one request stalls every chat on the worker and the health checks. The `index` property is blocking too (lists and describes indexes the first time, under a lock) and is only touched from those threads. Do not call `self.index.<anything>` from a coroutine. Methods:
  - `query(query, top_k, filter, include_metadata) -> list[SearchResult]`
  - `query_with_filter(query, document_type, bill_id, legislator_id, jurisdiction, top_k) -> list[SearchResult]`
  - `upsert_documents(documents: list[Document], batch_size=100) -> int`
  - `delete(ids=None, filter=None, delete_all=False) -> None`
  - `health_check() -> bool`
- **`Document`** — `id, content, metadata, embedding`
- **`SearchResult`** — `id, content, score, metadata`
- **`VectorStoreService.warm()`** (VOTEBOT-49) — opens the configured index and makes one `describe_index_stats` request, on a worker thread, so the first chat after a start pays for neither the index lookup nor the first connection. **It only reads**: unlike the `index` property (which creates a missing index, runbook 3.5), `warm` checks existence through `_open_existing_index` and returns False for a missing index without creating it. **`warm_up_services(settings)`** (`core/agent.py`) runs it and one tiny `embed("warm up")` (not `embed_query`, so nothing lands in the query-embedding cache) on the shared `_services_for(settings)` services; the `main.py` lifespan starts it as a background task (cancelled at shutdown, never awaited at startup), it never raises, and it logs `Warm-up finished` or `Warm-up incomplete` with which client failed.
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
    - Phase 4b: vote records (`bill-votes`, `legislator-votes`) — **legacy index only**: votes are not embedded in the canonical-id index (read live), so nothing searches for them there; legislator pages return no chunks there either (live from api-v3, no Webflow id lookup)
    - Phase 5: what changed, **only on changelog intent**. Legacy index: `bill-changelog` + webflow_id. Canonical-id index: **no search**: `_live_version_diffs(ocd_bill_id, query)` reads api-v3's stored `diff_from_previous_version` live (`BillVersionService.get_diffs`) under the same version scope as the text (current version, or the stage/date a query names), as synthetic `bill-version-diff` chunks (`source: "OpenStates (live)"`, labelled with both versions, capped at `DIFF_MAX_CHARS` per version) that `format_retrieved_chunks` groups as before
  - `_retrieve_organization_priority(query, filters, max_chunks) -> list[SearchResult]`
  - `_build_filters(page_context, query) -> dict` — builds Pinecone filter from page context; use this, never build filters inline. A bill is pinned by `webflow_id` on the legacy index and by `ocd_bill_id` on the canonical-id index (VOTEBOT-8). An organization is pinned by `webflow_id`/`slug` on the legacy index and by `broker_org_id` (an all-digit `page_context.id`, else the broker `slug`) on the canonical-id one (VOTEBOT-15, SYNC-91)
  - `_version_scope(ocd_bill_id, query) -> (current_document_id, version_filter)` — canonical-id index only. "Current" is **looked up** (via `BillVersionService`), never stored on vectors. Default filter `{"document_id": <current>}`; a query naming a stage/date gets `version_stage`/`version_date` `$in` filters instead; unknown current → no version filter (all versions, each labelled, and the formatter says no version is current). Applied to `bill-text`, `bill-version-diff` **and the "no typed results" fallback** — a version search is never widened to every version. A date counts as a version request only in a query that also says "version" or "draft"
  - `_ocd_mode` (property) — True when `settings.bill_filter_key == "ocd_bill_id"`. **The one switch**: it follows the index name, so there is no second setting to forget on rollback
  - `_identity_filter(filters) -> dict` — the bill-pinning part of a built filter, for follow-up queries on other document types (votes). Use it instead of reading `filters["webflow_id"]` directly
  - `_legislative_scope(page_context) -> dict` — `jurisdiction` (upper-case code) + `session_code` for a general page that names them; canonical-id index only. Governs `bill-text`/`bill-votes` **only**: the scoped query returns that jurisdiction's, and the unscoped query excludes those two types (`$nin`), so other jurisdictions' bills stay out while legislators/organizations (no `session_code`) stay in
  - `_lookup_ocd_bill_context(bill_info, page_context) -> PageContext | None` — canonical-id counterpart of `_lookup_bill_slug`: a bill named on a general page, found by `gov_id` + jurisdiction (+ session; the page's jurisdiction wins over one guessed from the query, which matches state names/codes as whole words only), reading `BILL_LOOKUP_TOP_K` chunks so the other session is seen. **Declines to guess** when the name matches more than one bill (every session has its own "HB 1"; session codes do not sort reliably as text, e.g. "2026D" after "2026")
  - `_deduplicate(results) -> list[SearchResult]` — the key is `(metadata["document_id"], hash(content[:500]))`: bill versions share most of their text, and merging identical passages across versions would erase which version they came from
  - `retrieve_for_bill(query, bill_id, jurisdiction) -> RetrievalResult`
  - `retrieve_for_legislator(query, legislator_id, jurisdiction) -> RetrievalResult`
  - `retrieve_general(query, jurisdiction) -> RetrievalResult`
- **`RetrievalResult`** — `chunks, query_used, filters_applied, total_retrieved, current_document_id` (the bill's current version on the canonical-id index, for the prompt builder to mark "current"; None otherwise)
- **`diff_scope_note(shown) -> str`** (VOTEBOT-22) — the one note retrieval adds naming exactly which version pairs the live diffs compare (and that earlier steps were not compared, and any diff cut for length). With **exactly one** comparison it also gives the model the heading to start with, `## What changed: <from> -> <to>` (a prompt rule alone left "Earliest to Latest Version" headings in 2 of 5 live runs); only when both versions have a recorded, non-blank name, with labels cleaned once (one line, quotes normalised) for both the heading and the comparison line. Change the wording here, not in the prompt text
- **`RetrievalConfig`** — `max_chunks, similarity_threshold, use_hybrid_search, deduplicate`
- **`ExtractedBillInfo`** — `bill_prefix, bill_number, jurisdiction`. Properties: `bill_id`, `slug_pattern`
- **`HybridRetrievalService`** — subclass of `RetrievalService`; keyword search stub, not yet implemented

**Concurrent retrieval (VOTEBOT-45).** The independent queries of a retrieval run at the same time: in `_bill_phases` (called through `_retrieve_bill_with_text_priority`, which cancels whatever is still running if a phase fails or the caller gives up) the bill-text, summary, the bill's own org chunks, the organization chunks and the live version-diff read are all started before the first result is awaited; `_retrieve_organization_priority` starts its phase 3 with phase 1 (phase 2 needs phase 1's top document, so it stays after); the scoped general-page path runs its two queries through `_query_together`. A query that used to size itself from an earlier result (`remaining_slots`, `remaining_org_slots`) is asked for the most it could have needed and the phase **cuts the score-sorted answer back** to the old size, so the chunks and their order are unchanged, up to how Pinecone orders results that tie at the cut (`tests/unit/test_retrieval_concurrency.py` replays golden outputs of the sequential code). `_release` cancels what is still running and **waits for it to end**; cancelling stops the wait for a query, not a Pinecone call already running on its thread. New retrieval queries: start them early and await them where the result is used; do not add a `await self.vector_store.query(...)` in the middle of the phases. The legacy-index vote queries are left serial (that index is frozen, VOTEBOT-17). `_version_scope` (the api-v3 version lookup, cached 120 s) still runs before the text query because the text filter needs it.

**Retrieval isolation rule**: `bill-text-history` and `bill-changelog` are invisible to all existing phases by design (explicit `document_type` filters). Only Phase 5 queries `bill-changelog` (legacy index), and only on changelog intent; on the canonical-id index `bill-version-diff` is not in the index at all and Phase 5 reads it live from api-v3. Never add unfiltered fallback queries that could surface these types in normal responses.

## Citations the model did not write (`core/citations.py`, VOTEBOT-21)

- **`chunks_used_by(answer, chunks, page_text) -> list[SearchResult]`** — the retrieved chunks an answer was built from: chunks sharing at least `MIN_SHARED_WORDS` (4) distinctive words (6+ letters, minus a small generic list and the page's own title/number) with the answer, best first, one per source+version, at most `MAX_CITATIONS` (3). Pure text, no model call.
- **`VoteBotAgent._cite_chunks_used(response, citations, retrieval_result, page_context)`** — called right after `_extract_citations` in BOTH `process_message` and `process_message_stream`: returns the model's own citations untouched when there are any, else builds `Citation`s from `chunks_used_by` over ONLY the chunks that carry the identity retrieval was pinned to (`retrieval_result.filters_applied`: `ocd_bill_id`, `broker_org_id`, ...). Organization/legislator chunks retrieved by similarity for other questions are never cited, and a general page (nothing pinned) gets no fallback. Off with `VOTEBOT_DETERMINISTIC_CITATIONS=false` (`settings.deterministic_citations`). A greeting, a refusal or an answer that says it does not know gets none. Do not add a second citation fallback elsewhere (the websocket recomputes confidence from the citations the stream returns, so it picks these up).

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
  - `health_check() -> bool` — a minimal completion (`responses.create`, "ping", 16 tokens), so readiness means chat can be served (a key without generation rights or an exhausted quota fails it), but **at most one real call a window** (VOTEBOT-51): the outcome is kept for `HEALTH_OK_TTL` (60 s), a failure for `HEALTH_FAIL_TTL` (10 s, so a recovery shows soon), and checks that arrive together share one call (a lock). `/health/ready` uses the **shared** `_services_for(settings).llm` so the kept result survives between requests (a new `LLMService()` per request would keep nothing). Do not replace the completion with a model lookup: it would pass for a key that cannot generate.
- **`_join_response_blocks(response) -> str`** — module-level helper. Replaces `response.output_text` to fix SDK block-boundary whitespace loss (`"".join()` drops `\n\n` at block boundaries). **Do not use `response.output_text` directly.**
- **`LLMResponse`** — `content, tokens_used, model, finish_reason, web_search_used, web_citations, response_id, bill_votes_tool_used, bill_votes_result`
- **`StreamChunk`** ⚠️ — `text, done, web_search_used`. **Name collision**: `api/schemas/chat.py` also has a class called `StreamChunk` (the WebSocket wire format). When importing, always verify which module you're pulling from. Consider renaming one of them in a future cleanup.
- **`WebSearchCitation`** ⚠️ — `url, title, snippet`. **Near-duplicate**: `api/schemas/chat.py` has `WebCitation` with the same three fields as a Pydantic model. One is a dataclass (internal), one is the API schema (external). There is a translation step between them. Don't add a third web citation shape.
- **`BillVotesToolResult`** — tool call result shape from `get_bill_info`
- **`LLMServiceFactory.get_instance()`** — singleton

## Agent (`core/agent.py`)

- **`_services_for(settings) -> _Services`** (VOTEBOT-37) — the routes build a `VoteBotAgent` per message, so the agent's services (`llm`, `retrieval` with its vector store and `BillVersionService`, `web_search`, `bill_votes`, `broker_lookup`, `legislators`) are built once per process and handed to every agent made with the same settings object (a different settings object, as in a test, gets its own). That is what lets a cache on a service (api-v3 bill versions 120 s / 45 s, `BillVotesService`'s party lookups, 1 h) survive to the next message and keeps one Pinecone/OpenAI/httpx client per process. **A service that caches must expire its entries**, because it now lives as long as the process; `WebflowLookupService` caches without expiry (legacy index), so it is NOT shared and is still built per agent. Do not construct these services inside a request handler.
- **`VoteBotAgent`** — orchestrates retrieval → augmentation → LLM → verification. Two entry points:
  - `process_message(message, session_id, page_context, navigation_context, conversation_history, channel, human_active, client_ip, user_agent, button, ...) -> AgentResult` — non-streaming (HTTP endpoint). `...` covers analytics-only kwargs (`visitor_id`, `conversation_id`, `session_message_index`, `conversation_message_index`, `entry_referrer`, `page_url`, `scroll_depth`, `time_on_page`) passed through to `_log_query`
  - `process_message_stream(...)` — same signature shape as `process_message` — streaming (WebSocket)
  - **`_gather_premodel_context(message, page_context, conversation_history, timings) -> PreModelContext`** (VOTEBOT-47) — steps 1 to 2h of the streaming answer: retrieval **and every lookup that does not read its result** (bill-info prefetch, legislator lookup, vote verification, org positions, org-bill positions, Webflow verification, the bill broker data) are started together and awaited after retrieval, so the wait before the model is the slowest of them, not the sum. They inherit the message's one `_enrichment_deadline`; a lookup that raises costs only its own context (logged, `""`); if retrieval fails or the caller gives up, `utils/tasks.py` `cancel_and_wait` cancels and waits for the rest. **A new pre-model lookup goes in here** (`start(name, lambda: ...)`, then await it where the others are), not as another serial `await` in `process_message_stream`. The bill-info trigger `_should_use_bill_votes_tool` is asked first with `rag_confidence=1.0`: it only ever adds cases as confidence drops (a test pins that), so a "yes" starts the prefetch immediately with no wasted call; a trigger that says yes only for a low confidence is asked again after retrieval and runs after it. Phase durations now overlap, so `phase_ms` adds up to more than the wait; read `marks_ms.model_call_start`. `process_message` (REST) is still serial.
  - Note: both entry points check the button cache first (`_maybe_serve_from_button_cache`) and return/yield the cached response without calling the LLM if it's a cache hit. No `llm.stream()`/`llm.complete()` call occurs on cache hits.
- **`_resolve_bill_from_title(message, page_context=None) -> (identifier, jurisdiction)`** — a bill named by title rather than number. Legacy index: top `bill` summary (reads `bill_prefix`/`bill_number`). Canonical-id index: top `bill-text` chunk (reads `gov_id`; the `bill` type does not exist there), limited to a general page's jurisdiction/session via `RetrievalService._legislative_scope`. Score must exceed 0.7 on both.
- **`_is_dispute_or_correction(message) -> bool`**, **`_phrase_in(text, phrase)`**, **`CORRECTION_PHRASES`** (VOTEBOT-29) — decides a message is the user disputing or correcting the bot; a dispute runs vote verification, **forces a web search** (`_should_use_web_search`) and adds the CMS or broker verification context, so a false positive costs time and money. Phrases match as **whole words** (`_phrase_in`), and every correction phrase names a person with a pronoun ("she is a", "he became"); never add a bare phrase such as "is the" or "became", which occur in ordinary questions. Tests: `tests/unit/test_dispute_detection.py`.
- **`_extract_jurisdiction_from_message(message) -> str | None`** (VOTEBOT-25, 28) — the jurisdiction guessed from message text; it is tried **before** the page's own state, so a wrong guess looks the bill up in the wrong state. Rules: state names match as whole words, longest first, with a hyphen, spaces or a line break allowed inside a two-word name ("West Virginia" is WV, not the "virginia" inside it); a two-letter code must be **written in capitals** ("VA HB 2724"; lower-case "in", "or", "me" are words); federal = whole-word federal(ly), congress(ional), U.S., United States, capital `US`, or lower-case "us" directly before bill/house/senate/hr (never the "us" inside "status" or the pronoun). Covers all 50 states; `retrieval.py`'s `STATE_MAPPINGS` (11 states) is a different, narrower table, not a substitute. Known and deliberate: a capitalised "IN HB 7089" reads as Indiana. Open: a trailing letter on the bill number ("HB 5601E") is dropped by the identifier patterns here and in retrieval (VOTEBOT-30)
- **`CACHED_ANSWER_CONFIDENCE`** (0.9, VOTEBOT-15) — the confidence of a saved quick-action answer, used by the agent's log and by the websocket's `stream_end` when the final chunk's metadata says `cached` (a saved answer ran no retrieval, so the websocket's own heuristic scored it about 0.2 lower than the same answer fresh). REST already used the agent's value
- **`AgentResult`** — `response, citations, confidence, requires_human, tokens_used, retrieval_count, cached, web_search_used, web_citations, response_id, bill_votes_tool_used, bill_votes_tool_duration_ms, bill_votes_result`. No `metadata` field on this dataclass (that's on `StreamChunkData`/`ResponseMetadata` instead).
- **`StreamChunkData`** — `text, done, citations, metadata`. Only 4 fields. There is no confidence/requires_human/web_search_used/bill_votes_tool_used field on this dataclass or on `ResponseMetadata` — `api/routes/websocket.py` recomputes `confidence` (`calculate_confidence()`) and `requires_human` (`check_human_handoff()`) itself from the accumulated response text once `done=True`, independent of `StreamChunkData`.

## Prompts (`core/prompts.py`)

- **`SYSTEM_PROMPT_BASE`** — base system prompt. Contains the bullet-per-line instruction. **Do not duplicate formatting rules inline.**
- **`BILL_CONTEXT_PROMPT`** — bill page context; includes changelog guidance ("cite version transition explicitly; say so if no changelog available")
- **`LEGISLATOR_CONTEXT_PROMPT`**, **`ORGANIZATION_CONTEXT_PROMPT`**, **`GENERAL_CONTEXT_PROMPT`** — context-specific prompt sections
- **`VERSION_CONTEXT_PROMPT`** (VOTEBOT-10) — bill pages only, appended after `BILL_CONTEXT_PROMPT`: answer from the version marked current unless another is asked for, name the version for every claim, answer "what changed" from "Changes in ..." sources naming both versions, and say so if the asked-for version is not in the sources
- **`LEGACY_LINK_EXAMPLES`, `CANONICAL_LINK_RULE`** (VOTEBOT-23) — the base prompt's example links show the old website (slug URLs: `/bills/education-funding-act`), which the new site cannot open (its `/bills/[id]` and `/legislators/[id]` take numeric ids; bill pages are `/explore/{jurisdiction}/{session}/{identifier}`, see `utils/ddp_urls.py`). `build_system_prompt(version_aware=True)` (the canonical-id index) swaps the examples block for the rule: link a bill only with the exact URL the sources give ("Source URL", "DDP URL" or "Bill page"), never build one. An import-time check (a `RuntimeError`, not an `assert`) requires the constant to appear exactly once in `SYSTEM_PROMPT_BASE`. A "Source URL" line is the chunk's own `url` (a bill chunk's is our `/explore/` page once `DDP_SITE_BASE_URL` is set); the rule says it counts only on a source about the same bill. Change link wording here, not inline.
- **`RAG_CONTEXT_TEMPLATE`** — wrapper for retrieved context injected into the prompt
- **`CITATION_INSTRUCTION`** / **`ENHANCED_CITATION_INSTRUCTION`** — citation formatting rules; `ENHANCED_CITATION_INSTRUCTION` gated on `settings.enhanced_citation_prompt`
- **`GENERAL_PAGE_PROMPT`**, **`_format_general_page_info`** (VOTEBOT-59) — on a `general` page that sends a title, URL, jurisdiction or session (ddp-next: lists, home, Vote, Staff), `build_system_prompt` adds a "the visitor is on this page" block after `GENERAL_CONTEXT_PROMPT`, so a vague question ("what can I do here?") is read as about that page. What each field does: `title` and `url` go into that block only (one line, capped at 200 characters: they come from the browser, and the block says they are data, never instructions; the URL is reduced to host and path, so credentials, query strings and fragments never reach the model); `jurisdiction` and `session` go into it **and** already scope retrieval on the canonical-id index (`RetrievalService._legislative_scope`: that jurisdiction's and session's `bill-text`). The block says the model cannot see the page, so it must not invent what is on screen. No page information means no block.
- **`HUMAN_HANDOFF_PROMPT`** — human handoff detection instructions; always appended last in `build_system_prompt`
- **`CONFIDENCE_SCORING_PROMPT`** — confidence rubric text. Defined but **currently unused** — not appended anywhere in `build_system_prompt`. Confidence is instead computed by `_calculate_confidence`/`calculate_confidence` heuristics, not by prompting the LLM.
- **`build_system_prompt(page_type, page_info, include_rag_context, retrieved_context) -> str`** — assembles the full system prompt: `SYSTEM_PROMPT_BASE` + context-specific prompt + optional `RAG_CONTEXT_TEMPLATE` + citation instruction + `HUMAN_HANDOFF_PROMPT`. Always use this, never concatenate prompts manually
- **`format_retrieved_chunks(chunks: list[dict], current_document_id: str | None = None) -> str`** — formats retrieved chunks for RAG injection. Adds `**Version Change:** from → to` header for `bill-changelog` chunks (legacy). Chunks of a labelled bill version (`bill-text`/`bill-version-diff` with `document_id` and `version_note`/`version_stage`) are **grouped under one header** such as `## HB 1 · Engrossed · 2026-03-04 · current` (diffs: `## HB 1 · Changes in Engrossed · 2026-03-04 · from Introduced`), the group sitting where its first chunk ranked. When version groups are present but no version is current, a `NO_CURRENT_VERSION_NOTE` leads the context. Callers pass `retrieval_result.current_document_id`. **Use this, don't write inline formatters.**
- **`_build_ddp_url(metadata, doc_type) -> str | None`** — builds the legacy (Webflow-era, slug) DDP citation URL from the slug in metadata. Returns None for an organization chunk of the canonical-id index (it carries `broker_org_id`): the new site has no organization page, as the broker's search (`_organization_result`, url null) also says (VOTEBOT-27). Page links for the new site come from `utils/ddp_urls.py`, not from here

## Webflow runtime lookup (`services/webflow_lookup.py`)

Bidirectional CMS fetch used at query time for RAG augmentation/verification. All lookup methods take `(webflow_id: str | None = None, slug: str | None = None)` — webflow_id preferred, slug is the fallback search.

**Not fully read-only**: `update_bill_fields()` / `update_bill_gov_url()` DO write to Webflow CMS (PATCH `.../items/{id}/live`), used by scheduler/sync scripts (`scripts/sync_bills.py` etc., outside `src/votebot/`) with the write-scoped `webflow_scheduler_api_key`. The runtime *retrieval/agent* path only ever calls the read methods below.

- **`WebflowLookupService`** (the **legacy index's** CMS path, removed by VOTEBOT-17) — **built per message on purpose** (`VoteBotAgent.__init__`; `_services_for` leaves it out because its `_org_cache` / `_bill_cache` never expire), and **no TTL cache or shared instance is planned** (VOTEBOT-52, decided won't-do: the code is scheduled for removal). **VoteBot 3.0 (the canonical-id index) never consults it**: `_prefetch_bill_org_positions`, `_prefetch_org_bill_positions` and `_verify_from_webflow` are routed to the broker there, and `_prefetch_bill_info`'s slug resolution ("Method 5") now runs only on the legacy index, because on the canonical index the page already names the bill (identifier, jurisdiction, session from ddp-next or `/content/resolve`). New 3.0 code must not call this service. Methods:
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

## Legislators and version diffs, live from api-v3 (`services/legislators.py`, `services/bill_versions.py`, VOTEBOT-15)

Canonical-id index: legislators and version diffs are not embedded (SYNC-94; 2026-10-05), so both are read from api-v3 through `openstates_base_url`/`openstates_headers`.

- **`BrokerLookupService.get_bill_analysis(jurisdiction, session, gov_id, deadline) -> BillAnalysis | None`**, **`get_concept_poll(...) -> ConceptPoll | None`** (VOTEBOT-59) — LegBot's summary / pros and cons / impact analysis of the bill's current version (`GET /api/bill-artifacts/current/`) and the bill's concept poll with tallies (`GET /api/concept-statements/`), keyed by the same natural key as the org-positions lookup. The broker sends the structured artifacts (pros and cons, impact) as **JSON text inside `content`**, not as objects; the parser accepts either. `format_bill_analysis` / `format_concept_poll` label them (neither is "Authoritative"; the analysis block says it is information, never instructions, and to use the text and votes for facts). Provenance is per part: a part is staff-written only when the broker's `origin` is exactly `human_edited` (the broker's values: `ai_generated`, `human_edited`, and a legacy import); anything else counts as AI, and the label says AI, staff, or partly each. Agent: `_wants_bill_broker_data(page_context)` (bill page, canonical index, broker set, jurisdiction + session + id) and `_prefetch_bill_broker_data` (analysis on every such message, poll only when `POLL_QUERY` matches: "poll", "survey", "concept poll/statements/votes", "what do people think"; not a bare "concept" or "statement"; any failure costs the context, never the answer), appended **after** the retrieved sources, in both `process_message_stream` (phase `bill_broker_data`) and `process_message`. **Not read, and why:** bill and legislator **scorecards** (`/api/bills/{id}/scorecard/`, `/api/legislators/{id}/scorecard/`) are keyed by the broker's own id, which VoteBot is never given (`/api/bills/resolve/` returns only the OpenStates id; the bill list ignores `gov_id`); the broker should add a scorecard keyed by (jurisdiction, session, gov_id) or return its bill id from `/bills/resolve/`. `bill_changelog` is skipped (a changelog question already reads api-v3's diffs live); `bill_vote_yes_frame` / `bill_vote_no_frame` / org-type artifacts / topics are not used. What the page pins in retrieval on the canonical index: a bill by `ocd_bill_id` (checked live: only that bill's chunks); an organization by `id` = the broker org id (digits) or `slug`, **not by name alone** (a name-only org page returned 10 other organizations' chunks); legislators are not in the index at all (SYNC-94), so a legislator page is answered from api-v3.
- **`BrokerLookupService.get_bill_scorecard(ocd_bill_id, deadline) -> BillScorecard | None`**, **`bare_uuid(value)`**, **`parse_bill_scorecard`**, **`format_bill_scorecard`** (VOTEBOT-60) — DDP's constituent-alignment scorecard of the page's bill, read by the **bare OpenStates UUID VoteBot already holds** (`GET /api/bills/{uuid}/scorecard/`, BROKER-325; the broker's own integer id is never given to VoteBot, and the old note that scorecards could not be read is obsolete). `bare_uuid` accepts the prefixed (`ocd-bill/`, `ocd-person/`) or upper-case spellings and returns `str(UUID(...))`: the broker 404s any other spelling, so **only that form is ever sent, and a value that is not a UUID sends nothing**. Semantics, from the broker's `scorecard_views_v2.py`: a vote's `districtChoice` is the **majority choice of that district's constituents on DDP**; `accountability` is `yes` when the legislator's vote matched it, `no` when it did not, `na` when either is missing; `score` = matched / (matched + against) x 100, `None` when no constituent votes exist. The scorecard lists only the **`eligible_for_scorecard` motions (one per chamber)**, so for "what happened in a vote" the live vote record wins (the formatted block says so). A bill's scorecard is large (76 KB for two motions), so `parse_bill_scorecard` keeps counts plus the first `SCORECARD_NAMES_SHOWN` (5) legislators who went against their district in each of the first `SCORECARD_MOTIONS_SHOWN` (4) motions, text one line and capped (about 1.5 KB). Fetched only when `asks_about_alignment(message)` holds (`accountab*`, `align*`, `scorecard`, "in line with" on their own; "constituents", "district" or "voters" only next to a word about voting or wanting, so "which districts does this bill affect?" is not a scorecard question) and the page holds a usable `ocd_bill_id`, in `_prefetch_bill_broker_data` with its own time budget **per lookup** (a slow or failing scorecard costs only itself; the analysis and poll that arrived are kept). `bare_uuid` strips at most one prefix. `_wants_bill_broker_data` now also holds for a bill page that has only the UUID.
- **`LegislatorLookupService`** — `find_by_id(person_id)`, `find_by_name(name, jurisdiction=None)` over `GET /people` (`include=offices&include=links`); None on any problem. `PeopleMatch(people, total)`: **api-v3's `pagination.total_items` counts joined rows, not people** (one person can be 3), so `total` is trusted only when more than one page exists; with `max_page == 1` it is `len(people)` (VOTEBOT-59). `Legislator` (`current` = has a current role), `format_legislators(people, asked)` (one profile, or a "several match, ask which" list; current members preferred over former).
- **`VoteBotAgent._legislator_context_from_api_v3(message, page_context)`** — legislator page: by `ocd-person/...` id, else title + jurisdiction; bill page: a name from `_legislator_name_in` (acronyms, `US_STATES` and `NAME_STOPWORDS` dropped; a cue word or at least two words); other pages only with a cue word (`LEGISLATOR_CUES`). **One time budget per message**: `_enrichment_deadline` (a contextvar set at the start of `process_message` and `process_message_stream`) is shared by the broker and legislator lookups; the broker service stops at it and returns what it has. An organization's bills are listed **once per bill** (the broker returns one row per bill version ordered by version id and exposes no timestamp, so the last row per bill wins). Used by both the streaming and non-streaming paths on the canonical index; the legacy index keeps `_prefetch_legislator_info`. `_candidate_person_name(message)` is the shared name guess.
- **`BillVersionService.get_diffs(ocd_bill_id, stages=(), dates=()) -> list[VersionDiff] | None`** — stored `diff_from_previous_version` for the current version (default) or the named stage/date, with the predecessor's label; not cached; every matching version is returned and retrieval's `DIFF_MAX_CHARS` budget (shared by all of them) decides what fits, saying how many were omitted. Since OPEN-118 api-v3 carries a diff for every classifiable version; one is missing mainly for a bill's first version, an unarchived predecessor, or a missing row.

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
  - **One shared HTTP client** (VOTEBOT-50): `BillVotesService._client()` yields one long-lived `httpx.AsyncClient` (made on first use, 30 s timeout, `Limits(max_connections=200, max_keepalive_connections=50, keepalive_expiry=30)`, a client whose event loop has ended is dropped and rebuilt, one whose loop is still running is refused with a `RuntimeError`; production has one loop) for OpenStates **and** Congress.gov, replacing the `async with httpx.AsyncClient(...)` per call; `close()` closes it and `close_services(settings)` (`core/agent.py`, called from the `main.py` lifespan, a no-op if no message was served) does so at shutdown. **No headers are set on the client**: the credential shape (`bearer` or `x-api-key`, never both) is chosen per request through `openstates_headers`. Measured on the dev api-v3: 10.1 ms per request with a new client each time, 4.3 ms reusing one. **Session fallback in `get_bill_info`**: the specified session first; only if it lacks the bill are the older ones (`year-1`, `year-2`, state bills with a numeric session only) asked, **together**, awaited in order so the newest older session that has the bill still wins, and a slower older one is cancelled once a newer one has answered. New OpenStates or Congress.gov calls use `async with self._client() as client`, not a new client.
  - **Party roster** (`_get_legislator_parties`, VOTEBOT-48): the name-to-party roster per jurisdiction (about 11 sequential 50-per-page requests for the federal one) is **served stale while revalidating**: a roster older than `PARTY_CACHE_SECONDS` (1 h) is returned at once and one background refresh (`_refresh_parties_in_background`, one per jurisdiction, its own short-lived client) replaces it. A refresh that fails, is partial (any page refused) or comes back empty changes nothing and is retried after `PARTY_RETRY_SECONDS` (60 s). Only a jurisdiction this process has not seen is fetched inline, and a first lookup that arrives while a refresh or the warm of it is running **joins that download** (re-reading the cache; inline only if it failed) instead of starting a second; `warm_parties(jurisdiction="us")` (called from the `main.py` lifespan, not awaited) fetches the federal one at startup. api-v3 answers 200 with an empty list, not an error, for a chamber a jurisdiction lacks. Do not add a second roster cache.
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
  - `log_event(...)` also takes `first_token_ms`, `phase_ms: dict[str,int]`, `marks_ms: dict[str,int]` (VOTEBOT-44, `query_processed` on the streaming path); omitted from the JSONL when unset.
  - `log_query(*, session_id, message, response, confidence, citations, page_context, channel, duration_ms, ...)` — legacy-format wrapper kept for backward compatibility; new code should use `log_event()`.
- `get_query_logger() -> QueryLogger | None` — module-level singleton accessor; returns `None` when `settings.query_log_enabled` is `False`
- **Do not log PII** — scrub tokens, credentials, and personal data before logging

## Redis store (`services/redis_store.py`)

Singleton: `get_redis_store() -> RedisStore`. All methods no-op gracefully when Redis is down (`connect()`/`disconnect()` called from `main.py` lifespan).

- **Chat sessions** (VOTEBOT-38) — `save_session(session_id, session)` / `load_session(session_id) -> dict | None`: the websocket session (history, handoff state, counters) as JSON under `votebot:session:{id}`, 2 h TTL refreshed on each save, so a visitor who reconnects to a DIFFERENT worker finds it. `ConnectionManager.save_session` (websocket.py) is the one writer: called after every client message, Slack event and disconnect, and it never overwrites a stored copy with a higher `revision` (a counter every save raises; `last_message_time` would miss a handoff or page change). `ConnectionManager.connect` loads it (Redis wins over this process's memory unless the local copy's revision is not lower, e.g. after a failed save). Redis calls time out after 2 s. Anything new that changes `sessions[...]` must be JSON-serialisable and reach one of those save points. With no Redis the sessions are per process, as before.
- **One socket per session id** (VOTEBOT-42) — `ConnectionManager.active_connections` maps a session id to ITS socket. `disconnect(session_id, websocket) -> bool` removes the entry only when it is still that socket and says whether it did; the endpoint's two cleanup branches (disconnect, error) call it first and `return` on False, because a newer connection (a reconnect with the same id registers before the old one's cleanup runs) now owns the session: no `conversation_ended`, no counter reset, no save from the old one. Never delete from `active_connections` by id alone, and keep slow work (Redis saves) after the guarded `disconnect`, not before it. Tests: `tests/unit/test_websocket_reconnect_race.py` (the endpoint driven with scripted fake sockets: reuse its `FakeSocket`).
- **Thread/session mapping** — `set_thread_mapping(thread_ts, session_id)` / `get_session_for_thread(thread_ts)` / `remove_thread_mapping(thread_ts)` — Slack human handoff cross-worker state. (Method names are `*_thread_mapping`/`get_session_for_thread`, not `set_thread_session`/`get_thread_session`.)
- **Button cache** — delegated to `ButtonCache`; don't interact with button Redis keys directly
- **Active jurisdictions** — `add_active_jurisdiction(code)` / `get_active_jurisdictions()`
- **Bill version cache** — `set_bill_version(webflow_id, version_data)` / `get_bill_version(webflow_id) -> dict | None`. Both live in this class; the sync/scheduler side (`scripts/sync_bills.py` etc., outside `src/votebot/`) writes via `set_bill_version`, VoteBot's agent/retrieval path only reads via `get_bill_version`.
- **Sync task/checkpoint storage** (used by scheduler/sync scripts, not the chat agent) — `set_sync_task`/`get_sync_task`, `add_sync_checkpoint`/`get_sync_checkpoints`/`copy_sync_checkpoints`
- **Scheduler leader election** (used by scheduler/sync scripts) — `acquire_scheduler_lock(worker_id)` / `refresh_scheduler_lock(worker_id)` / `release_scheduler_lock(worker_id)`, backed by `SCHEDULER_LOCK_KEY` with a 5-minute TTL
- **Pub/sub** — `publish_agent_event(event_type, session_id, payload)` / `subscribe_agent_events(handler)` on `"votebot:agent_events"` (Slack human handoff cross-worker); button cache invalidation (`"votebot:cache:invalidate"`) is handled separately in `button_cache.py`, not through this pub/sub pair

## Embeddings service (`services/embeddings.py`)

- **`EmbeddingService`** — OpenAI `text-embedding-3-large` (`EMBEDDING_DIMENSION = 3072`). Convenience methods: `embed_documents(texts) -> list[list[float]]`, `embed_query(text) -> list[float]` (**one embedding per distinct text** (VOTEBOT-46): a bounded in-memory LRU of 128 vectors, `QUERY_CACHE_MAX`, plus sharing of a request still in flight, so concurrent retrieval phases over the same text make one OpenAI call; a failed call is not kept; the returned list is shared, do not mutate it; a `Query embedding reused` debug line marks a reuse; do not add another query-embedding cache) (wrap the lower-level `embed(text) -> EmbeddingResult` / `embed_batch(texts, batch_size=100) -> list[EmbeddingResult]`). `get_dimension()` classmethod returns `EMBEDDING_DIMENSION`.
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
- **`utils/timings.py`** (VOTEBOT-44) — `PhaseTimings(start=None)`: per-message latency breakdown for the query log. `phase(name)` context manager (duration in ms, summed if entered twice, recorded even on exception) fills `phases_ms`; `mark(name)` (first call wins) / `get_mark(name)` record ms since the message started in `marks_ms`. Used by `process_message_stream` (phases `button_cache`, `retrieval`, `bill_info_prefetch`, `legislator_lookup`, `vote_verification`, `org_positions`, `org_bill_positions`, `webflow_verification`; marks `model_call_start`, `first_token`) and passed to `_log_query(timings=...)`. Not `MetricsCollector.timer` below: that is a process-wide aggregate with no per-message output. The non-streaming `process_message` is not instrumented.
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
| `bill-version-diff` | **Not embedded** (2026-10-05); Phase 5 reads it live (changelog intent only; canonical-id index) | api-v3's stored diff against the previous version, labelled with `from_version_note`/`from_document_id`; replaces `bill-changelog`. Every classifiable version has one (OPEN-118) except a bill's first and any whose predecessor is not archived; none for `unknown`-stage versions |
| `bill-votes` | Phase 4b | Vote records per bill |
| `legislator` | Standard retrieval | Legislator profiles |
| `legislator-votes` | Phase 4b (legacy index only) | Reverse index: per-legislator voting history |
| `organization` | Phase 4a, org retrieval | Org profiles with bill positions |
| `training` | General retrieval | Behaviour customisation docs |

## Bill versions (`services/bill_versions.py`, VOTEBOT-10)

- **`BillVersionService.get_versions(ocd_bill_id) -> list[BillVersion] | None`** — asks api-v3 for the bill's ordered versions (`/bills/ocd-bill/{uuid}?include=versions`, through `openstates_base_url`/`openstates_headers`), cached 120 s in-process; None when unavailable (remembered 45 s so a down api-v3 costs one 3 s wait, not one per message).
- **`BillVersion`** — `document_id` (api-v3's `archived_document_id` as a string, the vectors' `document_id`; None if not archived), `note, date, stage, ordinal`.
- **`current_version(versions) -> BillVersion | None`** — the latest **classifiable** version (stage not `unknown`), and only if it is archived; if the latest classifiable one has no archived document yet the answer is None, never an older version. api-v3's order (latest last) is trusted, never re-sorted. **Do not store an is_current flag on vectors and do not recompute version order or diffs here**: ordering is `api/version_ordering.py`, diffs are api-v3's stored `diff_from_previous_version`.

## Content resolution route (`api/routes/content.py`)

`GET /content/resolve?url=` turns a DDP URL into the page context the widget sends back on every message. Two paths, chosen by URL shape: ddp-next bill URLs (`/explore/{jurisdiction}/{session}/{gov_id}`, `/bills/{broker_id}`, `/bills/{jurisdiction}/{session}/{gov_id}`) go through ddp-broker-py (`_match_ddp_next_bill`, `resolve_ddp_next_bill`, `_broker_get`) and return `ocd_bill_id`; everything else is the Webflow CMS path (`fetch_webflow_item_by_slug`) returning `webflow_id`. ddp-next legislator/organization URLs are **deliberately unrecognised** (broker exposes no person id); `_broker_get` answers 502 for a non-JSON-object body. Open (VOTEBOT-31): an organization URL falls through to the Webflow path and answers 500 where no Webflow collection is configured (the new deployment); it should be a clear 4xx. **ddp-broker-py has no bill-detail endpoint** — use its `/api/bills/{id}/scorecard/` and `/api/bills/resolve/`. The websocket route builds `PageContext` from the payload in one place (`_page_context_from_payload`); a new page-context key must be added there or it is silently dropped.

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
| `pinecone_index_name` | `"votebot-large"` | Which index is read **and**, via `bill_filter_key`, how a bill is pinned in filters. Normalized (strip + lowercase; empty = default). Exactly `canonical_pinecone_index_name` (`ddp-knowledge-base`) → `ocd_bill_id`; every other name → `webflow_id`, with a startup warning (`Settings.index_is_recognized`). Rollback is this one value |
| `ddp_broker_api_root` | `""` | ddp-broker-py base URL for `/content/resolve` on ddp-next URLs; empty → 503 for those URLs only |

---

## Smoke test (`scripts/smoke_ws.py`, VOTEBOT-16)

Real `/ws/chat` client for cutover/rollback checks (`--cases` JSON, `--url`, `--index canonical|legacy`, `--retrieval`, `--expect-types`, `--forbid-types`); `check_turn`, `retrieval_checks`, `forbidden_types_check`, `discover_bill`, `run_case`, `main`. The websocket `stream_end` payload carries `bill_votes_tool_used` (from `ResponseMetadata`) so a vote question can be shown to have been answered by the live tool. Tests: `tests/unit/test_websocket_protocol.py` (route protocol with a `FakeAgent` and the FastAPI test client — **reuse that fixture** for any new websocket behaviour) and `tests/unit/test_smoke_ws.py` (script against a local uvicorn server). Don't write another websocket client for ops checks.

Load test: **`scripts/load_ws.py`** (VOTEBOT-35) starts **`scripts/load_app.py`** (the real app, only the LLM stubbed) with N uvicorn workers, opens C websockets (connecting over `--ramp` seconds; all at once lets one worker accept most of them), has each ask a button, a typed and a "what changed" question, and prints a table row: latency p50/p95, errors, `/health/live` latency under load (event-loop lag), answers per worker, peak memory. It writes to Redis db 5 and no query logs. Dev only. Use it before changing a worker count or any code on the chat path; do not write another load client.

Latency report: **`scripts/latency_report.py`** (VOTEBOT-53) reads the `query_processed` events of a query-log directory (`--log-dir`, `--days`, `--json`) and prints p50 / p90 / p95 with sample sizes of `duration_ms`, `first_token_ms`, `model_call_start` and every `phase_ms`, cut by page kind, live bill lookup, button and cache hit, and says which README targets are met. It needs no network and no keys and no Webflow; it is **not** `scripts/evaluate_production.py` (the legacy weekly quality job: citation rate, pass rate against Webflow ground truth, end-to-end latency only). Phases overlap since VOTEBOT-45/47: read `first_token_ms` and `model_call_start`, not the sum. Do not write another latency report.

---

## Ops scripts (`infrastructure/`)

- **Worker count** (VOTEBOT-39) — `WEB_CONCURRENCY` in `docker/prod.env.defaults` (uvicorn reads it; 1 today), `mem_limit: 1g` in `docker-compose.prod.yml`. Runbook 3.8 has the load-test table that sized them and how to change the count. Do not add a `--workers` flag to the Dockerfile command: it would override the setting.
- **`render-env.sh`** (VOTEBOT-14) — writes `/opt/votebot/.env` on the host from Secrets Manager (`ddp-sync/credentials`) plus `docker/prod.env.defaults`; `--check` validates and prints names only; `--with-slack` (cutover day) reads `votebot_slack_bot_token` / `votebot_slack_app_token`, never the bare `slack_bot_token`. A secret that is not valid JSON is reported with the decoder's line and column, never a value. Run `--check` after any edit to the secret.
- **`query-log-cleanup.sh`** (VOTEBOT-20) — compresses query logs older than 7 whole days and deletes those older than 365 (one JSONL file per day); NUL-safe, `--dry-run`, exits 1 on a failed operation. The operator installs it into `/etc/cron.daily` (runbook 3.6).
- **`start-votebot-dev.sh`** + **`launchd/com.ddp.votebot-dev.plist`** — the local dev VoteBot on the Mac Studio (`127.0.0.1:8010`, against the dev broker and local api-v3; README "Local dev copy"). The plist is a system LaunchDaemon definition; **`install-votebot-dev-daemon.sh`** (VOTEBOT-32) is what an admin runs with sudo to install it (`--check` changes nothing and needs no sudo, `--uninstall` removes it; it checks first, stops a hand-started copy holding the port, loads the plist and waits for `/health/ready`). Installed 2026-10-09 (the Mac Studio is rarely rebooted, so behaviour after a reboot has not been tried). Check `tests/unit/infrastructure/` before adding another script of this kind: every script here has static or behavioural tests there (the install script's use stubs for `launchctl`, so the real `launchctl bootstrap` is not covered).

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
