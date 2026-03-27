# VoteBot RAG Implementation: End-to-End Walkthrough

This document provides a comprehensive reference for VoteBot's Retrieval-Augmented Generation (RAG) system. The RAG system has two major phases: **Ingestion** (offline — getting data into the vector store) and **Retrieval + Generation** (online — answering user questions).

## Table of Contents

- [Phase 1: Ingestion (Writing to Pinecone)](#phase-1-ingestion-writing-to-pinecone)
  - [Step 1: Data Sources](#step-1-data-sources)
  - [Step 2: Metadata Extraction](#step-2-metadata-extraction)
  - [Step 3: Chunking](#step-3-chunking)
  - [Step 4: Embedding](#step-4-embedding)
  - [Step 5: Vector Store Upsert](#step-5-vector-store-upsert)
  - [Step 6: Pipeline Orchestration](#step-6-pipeline-orchestration)
  - [Step 7: Sync Handlers](#step-7-sync-handlers)
- [Phase 2: Retrieval + Generation (Answering Questions)](#phase-2-retrieval--generation-answering-questions)
  - [Step 8: Entry Point — WebSocket or REST](#step-8-entry-point--websocket-or-rest)
  - [Step 9: The Agent Pipeline](#step-9-the-agent-pipeline)
  - [Step 10: Multi-Phase Retrieval](#step-10-multi-phase-retrieval)
  - [Step 11: LLM Generation](#step-11-llm-generation)
  - [Step 12: Post-Processing](#step-12-post-processing)
  - [Step 13: Response Delivery](#step-13-response-delivery)
- [Key Packages Summary](#key-packages-summary)

---

## Phase 1: Ingestion (Writing to Pinecone)

This is the offline pipeline that takes raw content from various sources, chunks it, embeds it, and stores it in Pinecone.

### Step 1: Data Sources

**Location:** `src/votebot/ingestion/sources/`

Four source connectors fetch raw content:

| Source | File | Package | What it fetches |
|--------|------|---------|-----------------|
| **Webflow CMS** | `webflow.py` | `httpx` | Bills, legislators, orgs from Webflow REST API (`api.webflow.com/v2`). This is the primary source. |
| **OpenStates** | `openstates.py` | `httpx` | State + federal bill data, legislator profiles from `v3.openstates.org` |
| **Congress.gov** | `congress.py` | `httpx` | Federal legislation from `api.congress.gov/v3` |
| **PDF** | `pdf.py` | `PyPDF2` or similar | Legislative text PDFs (actual bill text) |

Each source implements an async generator `fetch(**config)` that yields `DocumentSource` objects (content string + metadata).

### Step 2: Metadata Extraction

**Location:** `src/votebot/ingestion/metadata.py`
**Package:** None (pure Python dataclasses)

`MetadataExtractor` normalizes raw API data into a `DocumentMetadata` dataclass with standardized fields:

- `document_id` — unique ID like `"bill-openstates-HB363"` or `"legislator-ocd-person/abc123"`
- `document_type` — one of: `bill`, `bill-text`, `bill-history`, `bill-votes`, `legislator`, `legislator-votes`, `organization`, `training`
- `source`, `title`, `jurisdiction`, `bill_id`, `legislator_id`, `url`
- `extra` dict — additional filterable fields (`webflow_id`, `slug`, `party`, `chamber`, `bill_prefix`, `bill_number`, etc.)

The `to_dict()` method flattens this into a Pinecone-compatible flat dictionary (no nested structures, no `None` values, lists converted to comma-separated strings).

### Step 3: Chunking

**Location:** `src/votebot/ingestion/chunking.py`
**Package:** `tiktoken` (OpenAI's tokenizer)

`ChunkingService` splits documents into chunks for embedding:

- **Config:** 750 tokens per chunk, 150 token overlap (from `config.py`)
- **Strategy 1 — Semantic chunking:** Splits on paragraph boundaries (`\n\n`), combines paragraphs until hitting the 750-token limit. Preserves logical sections.
- **Strategy 2 — Token-based fallback:** For content without clear paragraphs, splits at exact token boundaries with overlap.
- **HTML support:** `chunk_html()` uses `BeautifulSoup` to strip tags before chunking.
- **PDF support:** `chunk_pdf_text()` normalizes excessive whitespace/newlines first.

Each chunk is a `Chunk` dataclass with: `content`, `index`, `token_count`, `start_char`, `end_char`.

### Step 4: Embedding

**Location:** `src/votebot/services/embeddings.py`
**Package:** `openai` (AsyncOpenAI client), `tenacity` (retry logic)

`EmbeddingService` generates vector embeddings:

- **Model:** `text-embedding-3-large` (3072 dimensions)
- `embed_query(text)` — embeds a single query string, returns `list[float]`
- `embed_documents(texts)` — batch embeds multiple texts via `embed_batch()`, batches of 100
- Retries 3 times with exponential backoff on failure

### Step 5: Vector Store Upsert

**Location:** `src/votebot/services/vector_store.py`
**Package:** `pinecone` (Pinecone Python SDK), `tenacity`

`VectorStoreService` manages the Pinecone index:

- **Index:** `votebot-large`, serverless on AWS `us-east-1`, cosine similarity metric
- `upsert_documents(documents)`:
  1. For documents without embeddings, calls `EmbeddingService.embed_documents()`
  2. Builds Pinecone vectors: `{id, values (embedding), metadata (content[:40000] + all metadata fields)}`
  3. Upserts in batches of 100
- Content is stored inside Pinecone metadata (up to 40K chars) so it can be returned at query time without a separate store.

### Step 6: Pipeline Orchestration

**Location:** `src/votebot/ingestion/pipeline.py`
**Package:** `hashlib` (duplicate detection)

`IngestionPipeline` ties it all together:

1. Receives a `DocumentSource` (content + metadata)
2. Hashes content (SHA-256) for duplicate detection
3. Calls `ChunkingService.chunk_text()` to split into chunks
4. Creates `Document` objects with IDs like `"{document_id}-chunk-{index}"`
5. Calls `VectorStoreService.upsert_documents()` which embeds + upserts
6. Returns `IngestionResult` with stats

### Step 7: Sync Handlers

**Location:** `src/votebot/sync/handlers/`

The entry point for actual sync operations. The `UnifiedSyncService` dispatches to type-specific handlers:

| Handler | What it does |
|---------|-------------|
| `BillHandler` | Fetches bills from Webflow CMS, enriches with OpenStates data (legislative history, vote records, PDF text), creates multiple document types per bill (`bill`, `bill-text`, `bill-history`, `bill-votes`). Batch mode chains `BillVersionSyncService` after OpenStates history sync to check for newer bill text versions and update Webflow CMS `status`/`status-date` |
| `LegislatorHandler` | Fetches legislators from Webflow CMS + OpenStates, creates `legislator` and `legislator-votes` documents |
| `OrganizationHandler` | Fetches orgs from Webflow CMS with their bill positions, creates `organization` documents |
| `TrainingHandler` | Ingests static training docs (DDP FAQ, etc.) |
| `WebpageHandler` | Ingests arbitrary web pages |

Each handler uses `IngestionPipeline` for the chunk -> embed -> upsert flow.

**Batch sync memory management** (`BillHandler.sync_batch()` in `src/votebot/sync/handlers/bill.py`):
- Bills are processed and ingested **one at a time** — each bill's documents are ingested immediately, then references are dropped. This prevents accumulating all bill texts in memory.
- `gc.collect()` runs every 10 bills to reclaim pdfplumber objects and embedding vectors.
- PDF downloads are **streamed to a temp file** on disk (`aiter_bytes(chunk_size=65536)`) — no size limit, never buffered in memory. Large PDFs (e.g., federal omnibus bills) are fully supported.
- `pdfplumber` page layout caches are flushed after each page via `page.flush_cache()` during text extraction.
- Pinecone vectors are built and upserted **one batch at a time** (100 vectors) instead of assembling the full list in memory.

**Live progress reporting** (`SyncOptions.progress_callback`):
- The background sync runner creates an async `_progress()` closure that updates a live result dict after every item processed.
- The status endpoint (`GET /sync/unified/status/{task_id}`) reads from this dict, providing real-time `items_processed`, `items_successful`, `items_failed`, and `chunks_created` while the sync is running.
- Redis is updated every 10 progress calls (throttled) for cross-worker visibility.
- Bill handler calls `progress_callback` after each bill; legislator and org handlers call it once with final counts.

**Checkpoint/resume** (`SyncOptions.task_id` + `resume_task_id`):
- Each processed bill's Webflow ID is recorded in a Redis SET (`votebot:sync:checkpoint:{task_id}`, 24h TTL) via `RedisStore.add_sync_checkpoint()`.
- When a new sync request includes `resume_task_id`, checkpoints are copied from the old task to the new one (`RedisStore.copy_sync_checkpoints()`). The bill handler loads the checkpoint set and skips items already in it, counting them as processed+successful.
- This allows a crashed batch sync to resume from where it left off instead of re-processing from scratch.

**Sync scheduling**: Scheduled sync jobs (daily bill version checks, weekly legislator sync, monthly org sync, Voatz→Brevo sync, Webflow CMS batch jobs) are handled by [DDP-Sync](https://github.com/Digital-Democracy-Project/ddp-sync), a standalone service on port 8001. VoteBot no longer runs a scheduler — it is a chat-only service. DDP-Sync uses the same ingestion pipeline and sync handler code.

**Sync is triggered via:**
- DDP-Sync scheduled jobs (automatic, production)
- `POST /votebot/v1/sync/unified` (API endpoint for on-demand sync)
- CLI scripts in `scripts/` (e.g., `sync_bills.py`, `sync_legislators.py`)

---

## Phase 2: Retrieval + Generation (Answering Questions)

This is the online pipeline triggered when a user sends a message.

### Step 8: Entry Point — WebSocket or REST

**WebSocket** (`src/votebot/api/routes/websocket.py`): The chat widget connects via `ws://host/ws/chat`. The handler:
1. Parses `page_context` from the client payload (type, id, slug, webflow_id, jurisdiction, title)
2. Builds a `PageContext` object
3. Extracts conversation history from the session
4. Creates a `VoteBotAgent` and calls `process_message_stream()`

**REST** (`src/votebot/api/routes/chat.py`): `POST /votebot/v1/chat` does the same but calls `process_message()` (non-streaming).

### Step 9: The Agent Pipeline

**Location:** `src/votebot/core/agent.py`
**Package:** `structlog`, `asyncio`, `re`

`VoteBotAgent.process_message_stream()` runs this sequence:

#### Step 9a: RAG Retrieval
```
retrieval_result = await self.retrieval.retrieve(query=message, page_context=page_context)
```
Calls into `RetrievalService` (detailed in Step 10).

#### Step 9b: Bill Info Pre-fetch
If the query mentions a specific bill:
- `_should_use_bill_votes_tool()` checks for vote/bill keywords or bill number patterns (`HB`, `SB`, `HR`, etc.)
- `_prefetch_bill_info()` uses a **3-tier resolution** to find the bill:
  1. Regex extraction from message (e.g., "HR 1")
  2. Pinecone title search (e.g., "one big beautiful bill act")
  3. Conversation history search (e.g., "how did she vote on it?")
  4. Fallback to `page_context.id` on bill pages
- Calls `BillVotesService.get_bill_info()` -> OpenStates API
- If a legislator name is detected, calls `find_legislator_in_votes()` to extract their specific vote

#### Step 9c: Legislator Info Pre-fetch
If on a bill page and a person is mentioned:
- `_prefetch_legislator_info()` queries OpenStates `/people` API to get current role info
- Overrides stale LLM training data (e.g., "Ashley Moody is now a Senator")

#### Step 9d: Dispute Detection + Vote Verification
- `_is_dispute_or_correction()` checks for phrases like "that's wrong", "verify", "check again"
- If triggered, `_verify_legislator_vote()` goes directly to OpenStates for authoritative vote data

#### Step 9e: Webflow CMS Org Position Pre-fetch (bidirectional)
- On **bill pages**: `_prefetch_bill_org_positions()` — which orgs support/oppose this bill?
- On **org pages**: `_prefetch_org_bill_positions()` — which bills does this org support/oppose?
- Both use `WebflowLookupService` to query Webflow CMS directly (bypassing RAG)

#### Step 9f: Webflow CMS Verification (on disputes)
- `_verify_from_webflow()` fetches authoritative details from CMS for the current entity

#### Step 9g: Context Assembly
All the pre-fetched data is layered in priority order (most authoritative first):
```
1. webflow_verification_context  (CMS facts — top priority)
2. org_bill_positions_context     (CMS org<->bill relationships)
3. org_positions_context          (CMS bill->org positions)
4. vote_verification_context      (OpenStates votes)
5. legislator_info_context        (OpenStates current info)
6. bill_info_context              (OpenStates bill details)
7. retrieved_context              (RAG results — lowest priority)
```

#### Step 9h: System Prompt Construction
- `build_system_prompt()` from `prompts.py` assembles: base prompt + page-type-specific prompt + RAG context template + citation instructions

### Step 10: Multi-Phase Retrieval

**Location:** `src/votebot/core/retrieval.py`
**Package:** `structlog`, `re`

`RetrievalService.retrieve()` is the core retrieval orchestrator:

**Pre-processing:**
1. For `general` page context — tries to extract a bill ID from the query and upgrade to bill context
2. For `legislator` pages with slug but no ID — resolves OpenStates ID via `WebflowLookupService.get_legislator_details()`

**Filter Construction** (`_build_filters()`):
- Bill pages: filter by `webflow_id` (preferred) or `slug`
- Legislator pages: filter by `legislator_id` -> `webflow_id` -> `slug` (fallback chain)
- Organization pages: filter by `webflow_id` or `slug`

**Bill Retrieval** (`_retrieve_bill_with_text_priority()`): 6-phase pipeline:

| Phase | document_type filter | Purpose |
|-------|---------------------|---------|
| 1 | `bill-text` | Actual legislative PDF text (highest priority) |
| 2 | `bill` | CMS summary/description |
| 3 | `bill-history` | Legislative action timeline |
| 4a-i | `bill` (targeted query) | Bill's own chunks containing org position sections |
| 4a-ii | `organization` | Standalone org docs that reference this bill |
| 4b | `bill-votes` | Vote records |

Also detects **legislator follow-up questions** (e.g., "how about Rick Scott?") using a federal legislator cache, and looks up `legislator-votes` documents by person ID.

**Organization Retrieval** (`_retrieve_organization_priority()`): 3-phase:
1. Search `organization` docs (scoped by page context filters)
2. Fetch ALL chunks for the top-matching org (bill positions may be in a different chunk)
3. Fill remaining slots with general results

**All queries** go through `VectorStoreService.query()` which:
1. Embeds the query via `EmbeddingService.embed_query()`
2. Searches Pinecone with cosine similarity + metadata filters
3. Returns `SearchResult` objects with content, score, and metadata

Results are filtered by `similarity_threshold` (0.1) and deduplicated by content hash.

### Step 11: LLM Generation

**Location:** `src/votebot/services/llm.py`
**Package:** `openai` (AsyncOpenAI), `tenacity`, `json`

`LLMService` uses OpenAI's **Responses API** (not Chat Completions) for non-streaming, and Chat Completions for streaming:

- **Model:** `gpt-4.1`
- **System prompt** passed as `instructions` parameter
- **Tools available:**
  - `web_search_preview` — OpenAI's built-in web search (enabled when RAG confidence < threshold or dispute detected)
  - `get_bill_info` — custom function tool that calls `BillVotesService.get_bill_info()` (for non-streaming path)
- **Function calling loop:** Up to 3 iterations — if the LLM calls `get_bill_info`, the service executes it, returns the result, and continues the conversation
- **Streaming:** Uses Chat Completions API for regular streaming, Responses API for web-search-enabled streaming. Falls back to Chat Completions if Responses API streaming fails.

### Step 12: Post-Processing

**Location:** `src/votebot/core/agent.py`

**Citation extraction** (`_extract_citations()`):
- Scans the LLM response for `[Source: name](url)` and `[Source: name]` patterns
- Matches them to retrieved chunks by source name or document ID
- Only includes citations the LLM explicitly referenced (no implicit/automatic citations)

**Confidence scoring** (`_calculate_confidence()`):
- Base: 0.5
- +0.2 for having retrieved documents
- +0.05 per citation (up to +0.2)
- +0.1 * average relevance score
- +0.1 for web search used
- -0.15 for uncertainty phrases ("I'm not sure", "I don't know")

**Human handoff check** (`_check_human_handoff()`):
- Triggers on: explicit requests ("speak to a human"), frustration ("stupid bot"), low confidence (<0.3), legal advice requests

### Step 13: Response Delivery

- **WebSocket:** Chunks streamed as `stream_chunk` events, final metadata as `stream_end`
- **REST:** Full response returned as JSON with `{response, confidence, citations[]}`

### Step 14: Analytics Event Logging

**Location:** `src/votebot/services/query_logger.py`, `src/votebot/utils/intent.py`

After response delivery, three event types are logged to date-partitioned JSONL files via fire-and-forget (`asyncio.create_task`):

1. **`message_received`** — Emitted by the WebSocket handler before agent processing. Captures visitor identity (`visitor_id` from localStorage), conversation tracking (`conversation_id`, message indexes), and page context. Conversation boundary evaluation (inactivity, page change) happens before this event is emitted, ensuring correct `conversation_id` assignment.

2. **`query_processed`** — Emitted by `VoteBotAgent._log_query()` after response generation. Includes all behavioral/outcome fields:
   - **Intent**: Two-level classification (`primary_intent` + `sub_intent`) via `utils/intent.py` keyword heuristics
   - **Retrieval**: `retrieval_count`, `retrieval_sources` (normalized to controlled vocabulary from `MetadataExtractor` document types)
   - **Grounding**: `grounding_status` (`grounded`/`partial`/`ungrounded`) and `external_augmentation` (`none`/`web`) as independent dimensions
   - **Fallback**: `fallback_used` + `fallback_reason` (distinct from `web_search_used` — web search can be intentional, not just a fallback)
   - **Error path**: On processing failure, emitted with `error: true` + `error_type`

3. **`conversation_ended`** — Emitted by the WebSocket handler when a conversation boundary is detected or the session disconnects. Lightweight summary: `turn_count`, `duration_seconds`, `handoff_occurred`, `fallback_occurred`, `retrieval_miss_occurred`, `terminal_state`, `dominant_primary_intent`.

---

## Key Packages Summary

| Package | Used For |
|---------|----------|
| `openai` | Embeddings (text-embedding-3-large) + LLM generation (gpt-4.1) |
| `pinecone` | Vector storage and similarity search |
| `httpx` | All external API calls (Webflow, OpenStates, Congress.gov) |
| `tiktoken` | Token counting for chunking |
| `tenacity` | Retry with exponential backoff on all external calls |
| `structlog` | Structured logging throughout |
| `pydantic-settings` | Configuration from environment variables |
| `fastapi` | HTTP + WebSocket API framework |
| `redis` | Cross-worker session state, pub/sub, sync task state + checkpoints |
| `bs4` (BeautifulSoup) | HTML-to-text in chunking + ground truth tests |

---

## Planned Extension: Opinion Elicitation (Jigsaw)

The RAG pipeline will be extended with an opinion extraction layer that runs async post-response on bill-page messages (Step 14 analytics events already provide the trigger). Opinion extraction will:

1. Match user language against a **policy position landscape** per bill (PostgreSQL `opinion_landscapes`)
2. Generate stance scores (agree/disagree, -1 to +1) with confidence levels
3. Store extraction results as **OpinionSignal** records (PostgreSQL `opinion_signals`)
4. Feed into multi-position **opinion vectors** that accumulate across sessions

This does not modify the existing RAG retrieval or generation pipeline — it runs alongside it as an async post-processing step using cheap models (Haiku/4o-mini) to minimize cost impact.

See [plans/PLAN-jigsaw-overview.md](../plans/PLAN-jigsaw-overview.md) for the full system design and [plans/PLAN-jigsaw-stage-a.md](../plans/PLAN-jigsaw-stage-a.md) for the initial implementation plan.
