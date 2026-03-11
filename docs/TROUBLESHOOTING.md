# VoteBot Troubleshooting Guide

This document captures common issues, diagnostic procedures, and solutions for VoteBot's RAG and data sync systems.

## Table of Contents

- [Wrong Legislator Returned on Webflow Pages](#wrong-legislator-returned-on-webflow-pages)
- [Legislator Vote Lookups Not Working](#legislator-vote-lookups-not-working)
- [Model Contradicts Itself About Votes](#model-contradicts-itself-about-votes)
- [Poor Search Ranking for Full Name Queries](#poor-search-ranking-for-full-name-queries)
- [Organization Retrieval Issues](#organization-retrieval-issues)
- [Bill Identifier Extraction (HJR/SJR/HCR/SCR)](#bill-identifier-extraction-hjrsjrhcrscr)
- [Organization Chunk Data Quality](#organization-chunk-data-quality)
- [Corrupted Legislator-Votes Documents](#corrupted-legislator-votes-documents)
- [Missing Data in Search Results](#missing-data-in-search-results)
- [Federal Legislator Cache Issues](#federal-legislator-cache-issues)
- [Pinecone Index Diagnostics](#pinecone-index-diagnostics)
- [Webflow CMS Verification on Disputes](#webflow-cms-verification-on-disputes)
- [RAG Test Suite Diagnostics](#rag-test-suite-diagnostics)
  - [Failure Analysis (100-Document Sample)](#failure-analysis-100-document-sample)
- [Full Index Rebuild Procedure](#full-index-rebuild-procedure)
- [Human Handoff Messages Dropped in Multi-Worker Deployment](#human-handoff-messages-dropped-in-multi-worker-deployment)
- [Vote Lookup Fails on Legislator Pages for Specific Bills](#vote-lookup-fails-on-legislator-pages-for-specific-bills)
- [Wrong Vote Reported on Legislator Pages (LLM Ignores Data)](#wrong-vote-reported-on-legislator-pages-llm-ignores-data)
- [Bill Not Found When Referenced by Common Name](#bill-not-found-when-referenced-by-common-name)
- [Vote Lookup Fails on Bill Pages for Specific Legislators](#vote-lookup-fails-on-bill-pages-for-specific-legislators)
- [Wrong Organization Returned on Org Pages](#wrong-organization-returned-on-org-pages)
- [Sync Task Status Returns 404 in Multi-Worker Deployment](#sync-task-status-returns-404-in-multi-worker-deployment)
- [Chat Widget Truncated on Mobile (Send Button Cut Off)](#chat-widget-truncated-on-mobile-send-button-cut-off)
- [Production Query Monitoring](#production-query-monitoring)
- [DDP-Sync Issues](#ddp-sync-issues)
  - [Redis Health Check Error](#redis-health-check-error-redisstore-object-has-no-attribute-_redis)
- [Batch Sync Worker Killed Mid-Flight](#batch-sync-worker-killed-mid-flight)
  - [Sync Progress Reporting & Checkpoint/Resume](#sync-progress-reporting--checkpointresume-feb-2026-fix)
- [Scheduler Stops After Leader Worker Death](#scheduler-stops-after-leader-worker-death)
- [Bills With Empty Status Field in Webflow CMS](#bills-with-empty-status-field-in-webflow-cms)
- [Nightly Bill Sync Skips Bills for Certain States](#nightly-bill-sync-skips-bills-for-certain-states)

---

## Human Handoff Messages Dropped in Multi-Worker Deployment

### Symptom

Human agent replies in Slack are silently dropped. The agent types a reply in the Slack thread, but the user never sees it in the chat widget. Server logs show:

```
uvicorn[6163]: "Agent message received" thread_ts="1770510495.372949" agent="Ramon"
uvicorn[6163]: "No session found for thread" thread_ts="1770510495.372949"
```

### Root Cause

VoteBot runs with 2 uvicorn workers for concurrent request handling. The `thread_to_session` mapping (Slack thread_ts → WebSocket session_id) was stored in an **in-memory dict**, which is per-process:

1. **Worker A** handles the user's WebSocket connection and initiates the handoff → creates Slack thread → stores `thread_to_session[thread_ts] = session_id` in its own memory
2. **Worker B** receives the Slack Socket Mode event when the agent replies → looks up `thread_to_session[thread_ts]` → gets `None` because Worker B has a different in-memory dict
3. Worker B logs "No session found for thread" and silently drops the message

Similarly, `active_connections` (WebSocket objects) are inherently per-process — Worker B cannot send a message through Worker A's WebSocket even if it had the mapping.

### Fix (February 2026)

Added Redis-based cross-worker state via `src/votebot/services/redis_store.py`:

**A. Thread-to-session mapping** — Redis hash `votebot:threads`:
- `initiate_handoff()` writes to both the local dict (fast path) AND Redis
- `_handle_agent_message()` and `_handle_handoff_resolved()` look up the local dict first, then fall back to Redis

**B. Pub/sub for agent event delivery** — Redis channel `votebot:agent_events`:
- When Worker B receives a Slack event, it publishes an `agent_message` or `agent_left` event to Redis
- All workers subscribe to the channel; the worker that owns the WebSocket (`session_id in active_connections`) delivers the message
- Workers that don't own the connection silently ignore the event

**C. Graceful fallback** — All Redis methods no-op when `_client is None`. Single-worker deployments without Redis continue working via in-memory dicts (existing behavior).

### Architecture

```
Worker A (owns WebSocket)              Worker B (receives Slack event)
─────────────────────────              ──────────────────────────────
1. User confirms handoff
2. create_handoff_thread() → Slack
3. thread_to_session → Redis HSET
                                       4. Socket Mode event received
                                       5. Redis HGET → session_id
                                       6. Redis PUBLISH agent_message
7. Redis SUBSCRIBE receives event
8. Checks active_connections → found!
9. WebSocket send_json → user sees it
```

### Files Changed

| File | Change |
|------|--------|
| `src/votebot/services/redis_store.py` | NEW — Redis client wrapper (thread mapping + pub/sub + lifecycle) |
| `src/votebot/api/routes/websocket.py` | Redis lookup fallback in `_handle_agent_message` and `_handle_handoff_resolved`; pub/sub publish instead of direct send; `_handle_redis_event` subscriber; `_deliver_agent_message` and `_deliver_handoff_resolved` extracted |
| `src/votebot/main.py` | Redis `connect()` in lifespan startup, `disconnect()` in shutdown |

### EC2 Deployment

Redis must be installed on the EC2 instance:

```bash
sudo apt update && sudo apt install -y redis-server
sudo systemctl enable redis-server
sudo systemctl start redis-server
redis-cli ping  # Should return PONG
```

No changes to `.env` needed — default `redis_url=redis://localhost:6379/0` works.

### Diagnostic Steps

#### 1. Verify Redis is connected

```bash
sudo journalctl -u votebot | grep -i redis
```

Look for:
```
"Redis connected for cross-worker state" url="redis://localhost:6379/0"
```

If you see:
```
"Redis unavailable — falling back to in-memory state (single-worker only)"
```

Then Redis is not running or unreachable. Check:
```bash
redis-cli ping                    # Should return PONG
sudo systemctl status redis-server # Should be active
```

#### 2. Verify pub/sub subscriber is running

```bash
sudo journalctl -u votebot | grep "pub/sub"
```

Look for:
```
"Redis pub/sub subscriber started" channel="votebot:agent_events"
```

This should appear once per worker (so twice with 2 workers).

#### 3. Verify thread mapping is stored in Redis

After initiating a handoff, check Redis directly:

```bash
redis-cli HGETALL votebot:threads
```

Should show `thread_ts → session_id` mappings for active handoffs.

#### 4. Monitor Redis events in real-time

```bash
redis-cli SUBSCRIBE votebot:agent_events
```

Then have an agent reply in Slack — you should see the JSON event published.

#### 5. Check for delivery

```bash
sudo journalctl -u votebot | grep -E "agent_event|deliver|handle_redis"
```

Look for:
- `"Handling agent message callback"` — Slack event received
- `"Agent message delivered to user"` — Message sent via WebSocket

### Graceful Degradation

If Redis goes down:
- **Normal chat** continues working (Redis is only used for cross-worker handoff state)
- **Handoff in the SAME worker** continues working (local `thread_to_session` dict still used as fast path)
- **Handoff across workers** fails silently (agent messages dropped with "No session found for thread" log)
- **Fix**: Restart Redis (`sudo systemctl restart redis-server`), then restart VoteBot

### Prevention

- Monitor Redis with health checks: `GET /votebot/v1/health/ready` includes Redis status
- Ensure Redis is configured to start on boot: `sudo systemctl enable redis-server`
- Consider Redis persistence (RDB/AOF) if thread mappings must survive Redis restarts (not critical — handoffs are short-lived)

---

## Wrong Legislator Returned on Webflow Pages

### Symptom
When viewing a legislator page on the Webflow-hosted site (e.g., Mario Diaz-Balart), VoteBot returns information about a different legislator (e.g., Carlos Guillermo Smith). This happens even on fresh sessions with no prior conversation history.

### Example
```
User navigates to: digitaldemocracyproject.org/legislators/mario-diaz-balart-fl-representative
VoteBot: "Welcome! I can answer questions about Carlos Guillermo Smith..."
```

### Root Cause (Three-Layer Problem)

This bug had three contributing causes that all had to be fixed. Fixing retrieval alone was insufficient — the second and third issues were discovered during live production debugging.

#### Layer 1: Missing Pinecone Filter (retrieval.py)

Webflow page context only provides `slug` and `title` for legislators — it does NOT provide the OpenStates person ID (the `id` field). The retrieval filter in `_build_filters()` previously only filtered legislators by `page_context.id`:

```python
# OLD (broken)
elif page_context.type == "legislator" and page_context.id:
    filters["legislator_id"] = page_context.id
```

When `id` was `None`, **no Pinecone filter was applied**, so the vector search returned whichever legislator had the strongest semantic match to the query.

#### Layer 2: System Prompt Field Mismatch (agent.py — actual root cause)

Even after fixing retrieval, VoteBot still returned the wrong legislator. Server logs showed:
- Slug resolution: SUCCESS (correct OpenStates ID resolved)
- Pinecone filter: CORRECT (`legislator_id` set properly)
- Retrieved documents: CORRECT (10 chunks about Mario Diaz-Balart)

The real problem was in `_extract_page_info()` in `agent.py`. It stored the legislator name under the `"title"` key:

```python
# OLD (broken) — _extract_page_info() returned:
{"id": ..., "title": "Mario Diaz-Balart", "jurisdiction": ...}
```

But `_format_legislator_info()` in `prompts.py` looked for `info.get("name")`:

```python
# prompts.py line 231
if info.get("name"):
    parts.append(f"- Name: {info['name']}")
```

Result: The system prompt said **"No legislator details available"** — the LLM didn't know which legislator the user was asking about, even though the correct documents were retrieved.

#### Layer 3: Web Search Override

With "No legislator details available" in the prompt and a vague user message like "who is this guy?":
1. RAG confidence was **0.5** (below the legislator threshold of **0.7**)
2. This triggered the **web search fallback** via OpenAI Responses API
3. The web search had no legislator name to search for, so it returned a random Florida legislator (Carlos Guillermo Smith)
4. The LLM confidently presented the web search results as the answer

Server logs confirmed: `"web_search": true, "confidence_trigger": true` for the failing sessions.

### Fix (February 2026)

**Fix 1 — Slug → OpenStates ID resolution (`retrieval.py` + `webflow_lookup.py`):**

Added `_resolve_legislator_id()` method to `RetrievalService`. When a legislator page context has `slug` but no `id`, it calls `WebflowLookupService.get_legislator_details(slug=slug)` to look up the Webflow CMS item and extract the `openstatesid` field.

```python
# In retrieve(), before _build_filters():
if effective_context.type == "legislator" and not effective_context.id and effective_context.slug:
    resolved_id = await self._resolve_legislator_id(effective_context)
    if resolved_id:
        effective_context = PageContext(type="legislator", id=resolved_id, ...)
```

Added bill-style fallback chain for legislators in `_build_filters()`:

```python
elif page_context.type == "legislator":
    if page_context.id:
        filters["legislator_id"] = page_context.id
    elif page_context.webflow_id:
        filters["webflow_id"] = page_context.webflow_id
    elif page_context.slug:
        filters["slug"] = page_context.slug
```

Added `openstates_id` field to `LegislatorDetailsResult` in `webflow_lookup.py`.

**Fix 2 — Field mismatch (`agent.py` — critical fix):**

Added `"name"` key to the dict returned by `_extract_page_info()`:

```python
# NEW (fixed)
def _extract_page_info(self, page_context: PageContext) -> dict:
    return {
        "id": page_context.id,
        "name": page_context.title,    # <-- THIS was missing
        "title": page_context.title,
        "jurisdiction": page_context.jurisdiction,
        "session": getattr(page_context, "session", None),
        "url": page_context.url,
    }
```

Before fix: system prompt showed "No legislator details available"
After fix: system prompt shows "- Name: Mario Diaz-Balart"

This was the critical fix. With the legislator name in the system prompt, the LLM knows who the user is asking about even when RAG confidence is low, and web search (if triggered) has the correct name to search for.

### Resolution Flow

```
Webflow page → {type: "legislator", slug: "mario-diazbalart-fl0026us", title: "Mario Diaz-Balart"}
                                    ↓
              _resolve_legislator_id() → Webflow CMS lookup by slug
                                    ↓
              Gets openstatesid: "ocd-person/7e5729d1-198d-5389-be51-d1e05969729c"
                                    ↓
              _build_filters() → {legislator_id: "ocd-person/7e5729d1-..."}
                                    ↓
              Pinecone returns ONLY Mario Diaz-Balart's documents
                                    ↓
              _extract_page_info() → {"name": "Mario Diaz-Balart", ...}
                                    ↓
              System prompt: "- Name: Mario Diaz-Balart"
                                    ↓
              LLM knows the correct legislator (even if web search triggers)
```

### Diagnostic Steps

#### 1. Check if slug resolution is working

Look for these log entries in `sudo journalctl -u votebot`:
```
"Resolved legislator OpenStates ID from slug" slug=... openstates_id=... name=...
"Built retrieval filters" page_type=legislator filters={"legislator_id": "ocd-person/..."}
```

If you see `filters={}` for a legislator page, the resolution failed.

#### 2. Check if the system prompt includes the legislator name

Look for log entries showing the system prompt content. If you see "No legislator details available" for a legislator page, the `_extract_page_info()` → `_format_legislator_info()` chain is broken.

#### 3. Check if web search is overriding RAG results

Look for:
```
"web_search": true, "confidence_trigger": true
```

If web search triggers on a legislator page, it may indicate the RAG confidence is below the `legislator_threshold` (0.7). This is less of a problem now that the system prompt includes the legislator name, since the web search will at least search for the correct person.

#### 4. Check if Webflow CMS has the openstatesid field

```python
import asyncio
from votebot.config import get_settings
from votebot.services.webflow_lookup import WebflowLookupService

async def check_legislator_id(slug):
    service = WebflowLookupService(get_settings())
    result = await service.get_legislator_details(slug=slug)
    print(f"Found: {result.found}")
    print(f"Name: {result.name}")
    print(f"OpenStates ID: {result.openstates_id}")

asyncio.run(check_legislator_id("mario-diazbalart-fl0026us"))
```

#### 5. Verify the Webflow template sends slug

The Webflow legislator CMS template custom code should include:
```javascript
window.DDPChatConfig = window.DDPChatConfig || {};
window.DDPChatConfig.pageContext = {
    type: 'legislator',
    title: '{{wf {"path":"name","type":"PlainText"} }}',
    slug: '{{wf {"path":"slug","type":"PlainText"} }}'
};
```

### Comparison: Bill vs Legislator vs Organization Filter Chains

| Aspect | Bills | Legislators | Organizations |
|--------|-------|-------------|---------------|
| Primary filter | `webflow_id` | `legislator_id` (OpenStates ID) | `webflow_id` |
| Fallback 1 | `slug` | `webflow_id` | `slug` |
| Fallback 2 | — | `slug` | — |
| Pre-retrieval resolution | `_lookup_bill_slug()` (from query) | `_resolve_legislator_id()` (from Webflow CMS) | — (direct filter) |
| Context prompt | `BILL_CONTEXT_PROMPT` | `LEGISLATOR_CONTEXT_PROMPT` | `ORGANIZATION_CONTEXT_PROMPT` |

### Lessons Learned

1. **Check server logs before making assumptions**: The initial assumption was that Webflow wasn't sending the slug. Logs showed it was — the slug resolution worked perfectly.
2. **Field name mismatches are silent**: `info.get("name")` returning `None` doesn't raise an error — it just silently omits the legislator name from the prompt.
3. **Web search is a double-edged sword**: When RAG confidence is low and the system prompt lacks key context, web search can confidently return wrong information.
4. **Multi-layer bugs require end-to-end debugging**: Fixing retrieval alone was insufficient. The full chain (Webflow → retrieval → system prompt → LLM → web search) all had to work correctly.

### Prevention

- Ensure all Webflow CMS legislator items have the `openstatesid` field populated. Legislators without this field are skipped during ingestion (see `webflow.py:955-957`), so they won't have documents in Pinecone and the slug resolution will fail.
- When adding new page context fields, verify that `_extract_page_info()` maps them to the keys expected by the corresponding `_format_*_info()` function in `prompts.py`.

---

## Legislator Vote Lookups Not Working

### Symptom
VoteBot responds with "X is not listed as a voting member" or cannot find voting records for a legislator who should have votes in the system.

### Example
```
User: "How did Ashley Moody vote on HR1?"
Bot: "Ashley Moody is not listed as a voting member of the U.S. Congress..."
```

### Diagnostic Steps

#### 1. Check if the legislator exists in the index

```python
import asyncio
from src.votebot.config import get_settings
from src.votebot.services.vector_store import VectorStoreService

async def check_legislator(name):
    settings = get_settings()
    vs = VectorStoreService(settings)

    results = await vs.query(f'{name} legislator profile', top_k=5)
    for r in results:
        print(f'{r.score:.3f} | {r.metadata.get("document_id", "")}')
        print(f'  Content: {r.content[:200]}...')

asyncio.run(check_legislator("Ashley Moody"))
```

#### 2. Check if legislator-votes document exists

```python
async def check_votes_doc(person_uuid):
    settings = get_settings()
    vs = VectorStoreService(settings)

    doc_id = f'legislator-votes-{person_uuid}-chunk-0'
    result = vs.index.fetch(ids=[doc_id], namespace=vs.namespace)

    if doc_id in result.vectors:
        meta = result.vectors[doc_id].metadata
        print(f'Title: {meta.get("title")}')
        print(f'Total votes: {meta.get("total_votes")}')
        print(f'Yes votes: {meta.get("yes_votes")}')
        print(f'No votes: {meta.get("no_votes")}')
    else:
        print(f'Document not found: {doc_id}')

# Ashley Moody's OpenStates person UUID
asyncio.run(check_votes_doc("cb582ab6-6a5a-4578-9e44-620c9a6a1f4c"))
```

#### 3. Check if bill-votes contain the legislator's person ID

```python
async def check_bill_votes(person_uuid, bill_webflow_id):
    settings = get_settings()
    vs = VectorStoreService(settings)

    results = await vs.query(
        f'{bill_webflow_id} votes',
        top_k=20,
        filter={'document_type': 'bill-votes'}
    )

    for r in results:
        if bill_webflow_id in r.metadata.get('document_id', ''):
            if person_uuid in r.content:
                print(f'Person ID found in {r.metadata.get("document_id")}')
                idx = r.content.find(person_uuid)
                print(f'Context: {r.content[max(0,idx-30):idx+80]}')
            else:
                print(f'Person ID NOT in chunk {r.metadata.get("chunk_index")}')

asyncio.run(check_bill_votes("cb582ab6-6a5a-4578-9e44-620c9a6a1f4c", "682f4c9a5a8d551cb4777414"))
```

#### 4. Test search ranking

```python
async def test_search(query):
    settings = get_settings()
    vs = VectorStoreService(settings)

    results = await vs.query(query, top_k=10)

    for i, r in enumerate(results):
        doc_type = r.metadata.get('document_type', 'N/A')
        doc_id = r.metadata.get('document_id', '')
        print(f'{i+1}. {r.score:.3f} | {doc_type} | {doc_id[:60]}...')

asyncio.run(test_search("Ashley Moody vote HR1"))
```

### Common Causes

1. **Legislator-votes document doesn't exist**: The legislator's votes weren't extracted during the build process
2. **Corrupted duplicate documents**: Malformed documents ranking higher than valid ones (see next section)
3. **Missing person ID in bill-votes**: The bill sync didn't include the legislator's OpenStates person ID
4. **Federal legislator cache outdated**: The cache doesn't include newly appointed legislators
5. **Last-name-only in document**: Document contains only last name (e.g., "Moody") but user queries with full name ("Ashley Moody") - see [Poor Search Ranking](#poor-search-ranking-for-full-name-queries)

### Solutions

1. **Rebuild legislator-votes index**:
   ```bash
   python -m votebot.sync.build_legislator_votes
   ```

2. **Refresh federal legislator cache** (for federal legislators):
   ```bash
   python -m votebot.sync.federal_legislator_cache
   ```

3. **Re-sync bills with OpenStates data**:
   ```bash
   python -m votebot.updates.bill_sync batch --jurisdiction us --include-openstates
   ```

---

## Model Contradicts Itself About Votes

### Symptom
VoteBot gives contradictory answers about a legislator's vote within the same conversation:
- First response: "Ashley Moody voted No on HR1"
- Second response: "Ashley Moody is not listed as a member of Congress"
- Or: "All Republicans voted Yes" followed by claiming a Republican voted No

### Example
```
User: "How did Ashley Moody vote on this?"
Bot: "Ashley Moody voted No on this bill."

User: "Are you sure?"
Bot: "Ashley Moody is the Attorney General of Florida and does not serve in Congress..."
```

### Root Causes

1. **Duplicate votes in RAG data**: When a bill has multiple vote events (procedural votes, final passage), the same legislator may appear in both "Voted Yes" and "Voted No" sections for different motions. This confuses the model.

2. **Model hallucination**: When users challenge information, the model may fall back to outdated training data instead of trusting RAG results.

3. **Verification not triggered**: Phrases like "are you sure" or "be sure" may not trigger the verification flow.

### Solution: Vote Verification Feature

VoteBot includes automatic vote verification that fetches directly from OpenStates when users challenge information. This works from **any page type** (bill, legislator, organization) or even with no page context — not just bill pages.

This is triggered by phrases like:

- **Dispute phrases**: "that's wrong", "no way", "that can't be", "impossible"
- **Verification requests**: "be sure", "double check", "verify", "confirm"
- **Search commands**: "do a web search", "check openstates", "look it up"

When triggered, the agent:
1. Extracts the legislator name from the conversation (handles lowercase input, "X voted Y" patterns, etc.)
2. Gets the bill identifier from page context (if on a bill page) **or extracts it from the message text** (e.g., "are you sure Ashley Moody voted no on HR1?" → extracts "HR1"). Falls back to searching conversation history.
3. Gets the `session-code` from Webflow page context (e.g., "119" for 119th Congress)
4. Calls `BillVotesService.lookup_legislator_vote()` directly
5. **Prioritizes final passage votes** over procedural votes (motion to commit, cloture, etc.)
6. Returns authoritative data from OpenStates API that overrides RAG results

Additionally, when a dispute is detected on any page type, VoteBot fetches **Webflow CMS details** for the current page entity (bill, legislator, or organization) and injects them as authoritative context. See [Webflow CMS Verification on Disputes](#webflow-cms-verification-on-disputes) for details.

### Diagnostic Steps

#### 1. Check if verification was triggered

Look in the logs for these key messages:
```
"Checking dispute/verification trigger" message=... is_dispute=True/False
"Dispute detected, attempting vote verification"
"Vote verification successful" context_length=...
"Vote verification returned empty" (if name extraction or API lookup failed)
"Verifying legislator vote from OpenStates" legislator=... bill=... session=...
"Could not extract legislator name for vote verification" (if name not found in message or history)
"Could not extract bill identifier for vote verification" (if bill not found in message, page context, or history)
"Webflow CMS verification fetched" page_type=... (new — confirms CMS verification was injected)
"Webflow CMS verification: fetched bill details" name=... (detail lookup succeeded)
"Webflow CMS verification: fetched legislator details" name=... (detail lookup succeeded)
"Webflow CMS verification: fetched org details" name=... (detail lookup succeeded)
```

If `is_dispute=False` when you expected verification, the trigger phrase isn't being matched.

**Note**: Vote verification now works from any page type. If on a non-bill page, the bill identifier is extracted from the message text or conversation history using the same regex patterns as `retrieval.py`.

#### 2. Test verification manually

```python
import asyncio
from src.votebot.config import get_settings
from src.votebot.services.bill_votes import BillVotesService

async def verify_vote():
    settings = get_settings()
    service = BillVotesService(settings)

    result = await service.lookup_legislator_vote(
        legislator_name="Moody",
        jurisdiction="US",
        session="119",  # 119th Congress - use session-code from Webflow
        bill_identifier="HR1",
    )

    if result:
        print(f"Legislator: {result['legislator']}")
        print(f"Vote: {result['vote']}")  # Should be YES for final passage
        print(f"Motion: {result['motion']}")  # Should be final passage, not procedural
        print(f"Date: {result['date']}")
        # Check if multiple votes were found
        if result.get('total_votes_on_bill'):
            print(f"Total votes on bill: {result['total_votes_on_bill']}")
            print(f"Note: {result.get('note')}")
    else:
        print("Legislator not found in vote records")

asyncio.run(verify_vote())
```

#### 3. Check verification trigger phrases

The current trigger phrases are in `agent.py:_is_dispute_or_correction()`. If a phrase isn't triggering verification, it may need to be added to the list.

### If Verification Isn't Working

1. **Check OpenStates API key**: Ensure `OPENSTATES_API_KEY` is set and valid
2. **Check bill identifier format**: The bill must be in OpenStates (e.g., "HR1" for federal, "HB123" for state)
3. **Check legislator name extraction**: The lookup now searches for last name in addition to full name (e.g., "moody" matches "Moody (R-FL)")
4. **Check session resolution**: Session can come from multiple sources (see flow below)
5. **Check logs for session value**: Look for `bill_session=` in the WebSocket logs and `session=` in verification logs

### Session Resolution Flow

The session for OpenStates queries is resolved through this chain:

1. **Widget** calls `/content/resolve` with the DDP URL
2. **`/content/resolve`** fetches bill from Webflow CMS and extracts `session-code` field
3. **Widget** sends page_context to WebSocket including `session` from resolve response
4. **WebSocket handler** accepts both `session` and `session-code` field names
5. **If session is still null**, the agent calculates Congress number from year as fallback

### Webflow Page Context Fields

| Webflow Field | Resolve Response | WebSocket Accepts | Description |
|---------------|------------------|-------------------|-------------|
| `session-code` | `session` | `session` or `session-code` | OpenStates-friendly session (e.g., "119", "2025") |
| `bill-session` | (not used) | (not used) | Calendar year (integer, e.g. `2026`) — not used for OpenStates session matching |
| `jurisdiction` | `jurisdiction` | `jurisdiction` | State code or "US" for federal |
| `slug` | `slug` | `slug` | URL slug for the bill |

### Known Issues (Fixed)

#### Federal Bills Using Year Instead of Congress Number

**Bug**: The verification code was using the current year (e.g., "2026") as the session for federal bills, but OpenStates API expects the Congress number (e.g., "119").

**Example**: Query to `https://v3.openstates.org/bills/us/2026/HR1` would fail because the correct URL is `https://v3.openstates.org/bills/us/119/HR1`.

**Fix**:
1. The WebSocket handler now extracts `session-code` from Webflow and maps it to `page_context.session`
2. If `session-code` is not provided, the agent calculates the Congress number from the year as a fallback:
   - 119th Congress: 2025-2027
   - 120th Congress: 2027-2029

#### Name Extraction Failing for Lowercase Input

**Bug**: When users typed "how did ashley moody vote?" (lowercase), the name extraction couldn't find "Ashley Moody" because it only looked for capitalized names.

**Fix**: Added multiple extraction methods:
1. Pattern match for "X voted Y"
2. Pattern match for "Name (Party-State)"
3. Pattern match for "did X vote"
4. Fallback to capitalized word extraction

#### Verification Returning Procedural Vote Instead of Final Passage

**Bug**: When a legislator cast multiple votes on a bill (e.g., NO on "motion to commit" and YES on final passage), the verification returned the first match (procedural NO) instead of the more important final passage vote (YES).

**Example**: Ashley Moody voted NO on the "Motion to Commit HR 1 to Committee" but YES on final passage. The verification incorrectly reported her as voting NO.

**Fix**: The `lookup_legislator_vote` method now:
1. Collects ALL votes by the legislator on the bill
2. Scores each vote to prioritize final passage keywords over procedural keywords
3. Returns the highest-priority vote with a note indicating multiple votes exist

Final passage keywords (high priority): "final passage", "passage of the bill", "on passage", "third reading", "conference report"

Procedural keywords (low priority): "motion to commit", "motion to recommit", "cloture", "motion to table"

#### Legislator Name Not Matching Vote Records

**Bug**: When searching for "Ashley Moody" in vote records, no match was found because OpenStates stores names as "Moody (R-FL)" (last name with party/state).

**Example**: User asks about "Ashley Moody" → extracted name is "Ashley Moody" → search for "ashley moody" in "moody (r-fl)" fails because it's not a substring.

**Fix**: The `lookup_legislator_vote` method now builds multiple search terms:
- Full name: "ashley moody"
- Last name: "moody"
- First name: "ashley"

If ANY search term matches the vote record name, it's considered a match. This allows "moody" to match "Moody (R-FL)".

#### Session Not Extracted from Webflow CMS

**Bug**: The `/content/resolve` endpoint was looking for `session` or `legislative-session` fields in Webflow, but Webflow uses `session-code`.

**Fix**: The `extract_session` function now checks fields in this order:
1. `session-code` (Webflow's field name)
2. `session` (fallback)
3. `legislative-session` (fallback)
4. Extract year from slug and calculate Congress number (final fallback)

#### WebSocket Not Accepting Session from Resolve Endpoint

**Bug**: The WebSocket handler only looked for `session-code` in page_context, but `/content/resolve` returns the field as `session`.

**Fix**: WebSocket handler now accepts both field names:
```python
session=page_context_data.get("session") or page_context_data.get("session-code")
```

### Resolution Summary (February 2026)

The "Model Contradicts Itself About Votes" issue is now **RESOLVED**. The complete fix required addressing multiple issues:

1. **Name matching**: Search for last name ("moody") in addition to full name ("ashley moody") when looking up votes in OpenStates records
2. **Session extraction**: `/content/resolve` now extracts `session-code` from Webflow CMS
3. **Session passing**: WebSocket accepts both `session` and `session-code` field names
4. **Vote prioritization**: Returns final passage votes over procedural votes
5. **Congress number fallback**: Calculates "119" from year if session not provided
6. **Removed bill-page restriction**: Vote verification now works from any page type (legislator, organization, general) — bill identifier is extracted from message text or conversation history when not on a bill page
7. **Webflow CMS verification**: On disputes, authoritative CMS details (bill facts, legislator party/chamber/district, org type/website) are injected as context for all page types

**Verified working**: When users say "that's not true" or "check open states", VoteBot now correctly verifies Ashley Moody voted **YES** on HR1 final passage — from any page type, not just bill pages.

### Prevention

The duplicate votes issue can be mitigated by improving the `build_legislator_votes.py` to:
1. Only include final passage votes (not procedural)
2. Or clearly label each vote with its motion type

---

## Poor Search Ranking for Full Name Queries

### Symptom
The legislator-votes document exists and contains the correct data, but queries using the legislator's full name (e.g., "Ashley Moody vote HR1") don't return the document in top results. Queries using only last name (e.g., "Moody voting record") work fine.

### Example
```
Query: "Ashley Moody vote HR1"    -> Document NOT in top 15
Query: "Moody voting record HR1"  -> Document at position 1
```

### Root Cause
Vote records from OpenStates only include last names with party/state (e.g., "Moody (R-FL)"). When legislator-votes documents are built, they inherit this last-name-only format. The document title becomes "Moody (Republican-FL) - Voting Record" instead of "Ashley Moody (Republican-FL) - Voting Record".

Since semantic search relies on text similarity, queries with the full name "Ashley Moody" don't match well against documents that only contain "Moody".

### Diagnostic Steps

#### 1. Check if document has full name

```python
import asyncio
from src.votebot.config import get_settings
from src.votebot.services.vector_store import VectorStoreService

async def check_document_name(person_uuid):
    settings = get_settings()
    vs = VectorStoreService(settings)

    doc_id = f'legislator-votes-{person_uuid}-chunk-0'
    result = vs.index.fetch(ids=[doc_id], namespace=vs.namespace)

    if doc_id in result.vectors:
        meta = result.vectors[doc_id].metadata
        title = meta.get('title', '')
        content = meta.get('content', '')[:200]
        print(f'Title: {title}')
        print(f'Content start: {content}')

        # Check if it has only last name
        if title.startswith('Moody') and 'Ashley' not in title:
            print('\n⚠️  Document has last-name-only - needs rebuild with name enrichment')
    else:
        print('Document not found')

# Ashley Moody's UUID
asyncio.run(check_document_name("cb582ab6-6a5a-4578-9e44-620c9a6a1f4c"))
```

#### 2. Compare search rankings for full name vs last name

```python
async def compare_queries(full_name, last_name, person_uuid):
    settings = get_settings()
    vs = VectorStoreService(settings)

    doc_prefix = f'legislator-votes-{person_uuid}'

    for query in [f'{full_name} vote HR1', f'{last_name} voting record HR1']:
        results = await vs.query(query, top_k=15)
        position = None
        for i, r in enumerate(results):
            if doc_prefix in r.metadata.get('document_id', ''):
                position = i + 1
                break
        print(f'Query "{query}": Position {position if position else "NOT FOUND"}')

asyncio.run(compare_queries("Ashley Moody", "Moody", "cb582ab6-6a5a-4578-9e44-620c9a6a1f4c"))
```

### Solution

The `build_legislator_votes.py` module enriches legislator names from the federal legislator cache. Rebuild the index to apply name enrichment:

```bash
# Rebuild legislator-votes documents with full names
python -m votebot.sync.build_legislator_votes
```

The rebuild will:
1. Look up each legislator's person ID in the federal legislator cache
2. Replace last-name-only entries with full names (e.g., "Moody" → "Ashley Moody")
3. Include full names in document titles and content

After rebuild, verify the fix:
```python
# Should now show full name in title
asyncio.run(check_document_name("cb582ab6-6a5a-4578-9e44-620c9a6a1f4c"))
# Expected: "Ashley Moody (Republican-FL) - Voting Record"
```

### Prevention

The name enrichment feature is built into `build_legislator_votes.py`:
- Uses `federal_legislator_cache.get_by_person_id()` to look up full names
- Automatically enriches federal legislators during document creation
- Reports `name_enrichments` count in build results

Ensure the federal legislator cache is up-to-date before building:
```bash
python -m votebot.sync.federal_legislator_cache
python -m votebot.sync.build_legislator_votes
```

---

## Organization Retrieval Issues

### Symptom
VoteBot fails to answer questions about organization positions on bills, or returns incorrect organization types. Common failure patterns:

1. **Bill→Org queries**: "Which organizations support HB 123?" returns generic info instead of org positions
2. **Org type queries**: "What type of organization is ACLU?" returns paraphrased or incorrect type
3. **Org→Bill queries**: "What bills does Veterans for All Voters support?" returns incomplete or no results

### Root Causes

#### Bill→Org: Retrieval pipeline filters out org data
When a bill identifier is detected in a query, the retrieval pipeline activates bill-priority mode, which searches only for `document_type="bill"`, `"bill-text"`, `"bill-history"`, and `"bill-votes"`. Organization documents are excluded by these filters.

**Fix history:**

**Phase 4a-i + 4a-ii (Pinecone retrieval, February 2026)**: Added two-phase organization retrieval to `_retrieve_bill_with_text_priority()` in `retrieval.py`. Phase 4a-i searches the bill's own `document_type="bill"` chunks with a targeted semantic query for org positions. Phase 4a-ii falls back to standalone `document_type="organization"` docs. This improved bill→org from **58.9% to 82.1%**, but 20/112 tests still failed due to:
- 13 bills with org data in Pinecone but similarity scores (0.35-0.56) below the 0.7 threshold
- 4 bills with NO org position data in Pinecone at all
- 3 edge cases (training data collision, wrong bill matched)

**Webflow CMS Runtime Lookup (February 2026)**: Bypasses Pinecone entirely by fetching org positions directly from Webflow CMS at runtime. The `WebflowLookupService` (`services/webflow_lookup.py`) pre-fetches authoritative org data before LLM generation, similar to the vote verification pre-fetch pattern. This resolved the remaining failures, improving bill→org from **82.1% to 99.1%** (111/112 passed).

How it works:
1. `agent.py` detects org-related queries via `_is_org_position_query()` (same keyword list as `retrieval.py:314-319`)
2. If the query is on a bill page (`page_context.type == "bill"`), `_prefetch_bill_org_positions()` is called
3. `WebflowLookupService.get_bill_org_positions(webflow_id, slug)` fetches the bill from Webflow CMS
4. Extracts `member-organizations` and `organizations-oppose` reference ID lists from `fieldData`
5. Resolves each org ID to `{name, type, slug}` via parallel `asyncio.gather` calls with in-memory caching
6. Formats as markdown labeled "Authoritative Source — Webflow CMS" and prepends to LLM context

Configuration:
- Feature flag: `WEBFLOW_ORG_LOOKUP_ENABLED` (default: `true`)
- Requires `webflow_id` or `slug` in `page_context` (the chat widget provides these via `/content/resolve`)
- Graceful degradation: if Webflow API fails, falls back silently to existing Pinecone retrieval

#### Org type: Unfiltered semantic search
Generic queries about an organization (e.g., "What type of organization is X?") go through the default semantic search, which may return random documents instead of the target org's profile.

**Fix 1 (implemented February 2026)**: Added `_is_organization_query()` detection and `_retrieve_organization_priority()` to `retrieval.py`. This:
1. Detects org-focused queries by checking for strong indicators ("organization", "nonprofit", "501(c)")
2. Routes to a dedicated retrieval path that searches `document_type="organization"` first
3. Fetches ALL chunks for the top matching org by `document_id` to capture bill positions in separate chunks

**Fix 2 (implemented February 2026)**: Added page-context awareness for organization pages. When the user is ON an org page, retrieval is now scoped by `webflow_id`/`slug` — see [Wrong Organization Returned on Org Pages](#wrong-organization-returned-on-org-pages) for full details. This fixed the case where generic queries like "tell me about this org" returned the wrong organization because the query didn't match org keyword patterns.

#### Org→Bill: Webflow CMS Runtime Lookup (February 2026)
Organization documents in Pinecone are chunked aggressively. The org header (name, type, description) often ends up in one chunk (~25 characters), while bill positions are in separate chunks. Some bill positions may be missing entirely from the index. This caused ~10 test failures out of ~259 org tests.

**Fix**: Mirrors the bill→org Webflow CMS runtime lookup in the reverse direction. When a user is on an organization page and asks about bills, the agent pre-fetches the org's `bills-support` and `bills-oppose` reference fields directly from Webflow CMS, resolves each bill ID to its name/identifier, and prepends as authoritative context before LLM generation.

How it works:
1. `agent.py` detects bill-related queries via `_is_bill_position_query()` (keywords: "bill", "bills", "support", "oppose", "legislation", etc.)
2. If the query is on an org page (`page_context.type == "organization"`), `_prefetch_org_bill_positions()` is called
3. `WebflowLookupService.get_org_bill_positions(webflow_id, slug)` fetches the org from Webflow CMS
4. Extracts `bills-support` and `bills-oppose` reference ID lists from `fieldData`
5. Resolves each bill ID to `{name, identifier, slug}` via parallel `asyncio.gather` calls with `_bill_cache`
6. Formats as markdown labeled "Authoritative Source — Webflow CMS" with DDP `/bills/{slug}` links
7. Prepends to LLM context before RAG results

Configuration:
- Reuses existing `WEBFLOW_ORG_LOOKUP_ENABLED` feature flag (both directions share the same mechanism)
- Requires `webflow_id` or `slug` in `page_context` (the chat widget provides these via `/content/resolve`)
- Graceful degradation: if Webflow API fails, falls back silently to existing Pinecone retrieval
- `_bill_cache` prevents duplicate API calls for shared bill references

**Result**: Org→bill tests improved from ~96% to **99.3%** (290/292), with supported_bills and opposed_bills at **100%** (99/99).

### Diagnostic Steps

#### 1. Check if org documents exist for an organization

```python
import asyncio
from src.votebot.config import get_settings
from src.votebot.services.vector_store import VectorStoreService

async def check_org(org_name):
    settings = get_settings()
    vs = VectorStoreService(settings)

    results = await vs.query(
        f'{org_name} organization profile',
        top_k=5,
        filter={'document_type': 'organization'}
    )
    for r in results:
        doc_id = r.metadata.get('document_id', '')
        print(f'{r.score:.3f} | {doc_id}')
        print(f'  Content ({len(r.content)} chars): {r.content[:200]}...')

asyncio.run(check_org("ACLU"))
```

#### 2. Check chunk sizes for an organization

```python
async def check_org_chunks(org_slug):
    settings = get_settings()
    vs = VectorStoreService(settings)

    all_ids = []
    for ids in vs.index.list(namespace=vs.namespace, prefix=f'organization-{org_slug}'):
        all_ids.extend(ids)

    print(f'Total chunks for {org_slug}: {len(all_ids)}')
    if all_ids:
        result = vs.index.fetch(ids=all_ids[:10], namespace=vs.namespace)
        for doc_id, vec in result.vectors.items():
            content = vec.metadata.get('content', '')
            print(f'  {doc_id}: {len(content)} chars')
            if len(content) < 50:
                print(f'    ⚠️ Very small chunk: "{content}"')

asyncio.run(check_org_chunks("aclu"))
```

### Test Validation

The RAG test suite uses `contains_any` validation with `min_matches` for org-related fields:
- **Org type**: `contains_any` with `org_type_keywords` (min_matches: 1) — tolerates LLM paraphrasing
- **Bill title**: `contains_any` with `name_keywords` (min_matches: 2) — matches keyword subsets

---

## Bill Identifier Extraction (HJR/SJR/HCR/SCR)

### Symptom
Queries about joint or concurrent resolutions (e.g., "Tell me about HJR 7") fail to trigger bill-priority retrieval. The system treats them as general queries instead.

### Root Cause
The bill extraction regex in `retrieval.py` originally only matched common patterns like HB, SB, HR, HJ, SJ. Joint resolutions (`HJR`, `SJR`) and concurrent resolutions (`HCR`, `SCR`) were not included.

### Fix (February 2026)
Updated the regex pattern in `_extract_bill_identifier()` to include longer patterns **before** shorter ones (important for correct matching):

```python
pattern1 = r'\b(H\.?J\.?R\.?|S\.?J\.?R\.?|H\.?C\.?R\.?|S\.?C\.?R\.?|H\.?B\.?|S\.?B\.?|H\.?R\.?|S\.?|H\.?J\.?|S\.?J\.?)\s*(\d+)\b'
```

Key points:
- `HJR`, `SJR`, `HCR`, `SCR` patterns are listed **before** shorter `HJ`, `SJ` patterns
- This prevents `HJR 7` from matching as `HJ` + `R7` instead of `HJR` + `7`
- Supports dotted variants: `H.J.R. 7`, `S.C.R. 12`, etc.

### Verification
```bash
# Test with a joint resolution query
PYTHONPATH=src python -c "
import asyncio
from votebot.core.retrieval import RetrievalService
from votebot.config import get_settings

settings = get_settings()
rs = RetrievalService(settings)
result = rs._extract_bill_identifier('Tell me about HJR 7')
print(f'Extracted: {result}')
# Expected: ('HJR', '7')
"
```

---

## Organization Chunk Data Quality

### Symptom
Organization documents in Pinecone have very small chunks (sometimes only 25 characters) containing just the org header, while bill positions are in separate chunks or missing entirely.

### Example
```
Chunk 0: "# Veterans for All Voters"  (25 chars — just the header)
Chunk 1: "## Bills Supported\n- One Big Beautiful Bill Act..."  (if it exists)
```

When a query like "What bills does Veterans for All Voters support?" retrieves only the header chunk, there's no bill position data to answer from.

### Root Cause
The text chunking pipeline (`ingestion/chunking.py`) splits on markdown headers. For organizations with short descriptions, the first chunk contains only the `# Organization Name` header. Bill position lists end up in subsequent chunks that may not rank highly in semantic search.

### Current Mitigation
The `_retrieve_organization_priority()` method in `retrieval.py` mitigates this by:
1. Finding the top-matching org document
2. Fetching ALL chunks for that org by `document_id` prefix
3. Including all chunks in the context, not just the semantically closest ones

### Long-Term Fix
Re-ingest organization documents with one of these strategies:
1. **Larger chunk size**: Increase minimum chunk size so header + description + bill positions stay together
2. **Prepend org name**: Add the organization name to every chunk so they're all semantically linked
3. **Structured metadata**: Store bill positions as metadata rather than chunk content

To re-ingest:
```bash
# Re-sync all organizations
python scripts/sync.py organization --batch --clear-namespace

# Or rebuild the full index
python scripts/rebuild_pinecone.py --content-types organization --yes
```

---

## Corrupted Legislator-Votes Documents

### Symptom
Valid legislator-votes documents exist but aren't appearing in search results because corrupted duplicates with similar content are ranking higher.

### Root Cause
When bill-votes content is split across Pinecone chunks, the regex parser can match partial person IDs at chunk boundaries, creating malformed entries like:
- `ocd-person/cb582ab6-6a` (truncated at chunk boundary)
- `44-620c9a6a1f4c` (partial UUID fragment)
- `PA), [ocd-person/cb582ab6...` (garbage prefix from prior entry)

These malformed IDs create fake `legislator-votes` documents that have similar content to valid documents, causing them to rank highly in semantic search.

### Diagnostic Steps

#### 1. Check for corrupted document IDs

```python
import asyncio
import re
from src.votebot.config import get_settings
from src.votebot.services.vector_store import VectorStoreService

async def find_corrupted():
    settings = get_settings()
    vs = VectorStoreService(settings)

    # Valid patterns
    valid_uuid = re.compile(
        r'^legislator-votes-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}(-chunk-\d+)?$'
    )
    valid_name = re.compile(r'^legislator-votes-[a-z\-]+(-chunk-\d+)?$')

    all_ids = []
    for ids in vs.index.list(namespace=vs.namespace, prefix='legislator-votes-'):
        all_ids.extend(ids)

    corrupted = [
        id for id in all_ids
        if not valid_uuid.match(id) and not valid_name.match(id)
    ]

    print(f'Total: {len(all_ids)}, Corrupted: {len(corrupted)}')
    for id in corrupted[:20]:
        print(f'  {repr(id)}')

asyncio.run(find_corrupted())
```

#### 2. Check search results for a specific legislator

Compare search results to see if corrupted documents are outranking valid ones:

```python
async def check_ranking(legislator_name, correct_uuid):
    settings = get_settings()
    vs = VectorStoreService(settings)

    results = await vs.query(f'{legislator_name} voting record', top_k=10)

    correct_doc = f'legislator-votes-{correct_uuid}'
    found_position = None

    for i, r in enumerate(results):
        doc_id = r.metadata.get('document_id', '')
        is_correct = correct_doc in doc_id
        marker = ' *** CORRECT ***' if is_correct else ''
        if is_correct:
            found_position = i + 1
        print(f'{i+1}. {r.score:.3f} | {doc_id[:60]}...{marker}')

    if found_position:
        print(f'\nCorrect document at position {found_position}')
    else:
        print('\nCorrect document NOT in top 10!')

asyncio.run(check_ranking("Moody", "cb582ab6-6a5a-4578-9e44-620c9a6a1f4c"))
```

### Solution

#### 1. Run the cleanup command

The `build_legislator_votes.py` module includes a cleanup function:

```bash
# Dry run to see what would be deleted
python -m votebot.sync.build_legislator_votes --cleanup --dry-run

# Actually delete corrupted documents
python -m votebot.sync.build_legislator_votes --cleanup
```

#### 2. Rebuild from scratch

For a complete fix, delete all legislator-votes and rebuild:

```python
import asyncio
from src.votebot.config import get_settings
from src.votebot.services.vector_store import VectorStoreService

async def delete_all_legislator_votes():
    settings = get_settings()
    vs = VectorStoreService(settings)

    all_ids = []
    for ids in vs.index.list(namespace=vs.namespace, prefix='legislator-votes-'):
        all_ids.extend(ids)

    print(f'Deleting {len(all_ids)} documents...')

    batch_size = 100
    for i in range(0, len(all_ids), batch_size):
        batch = all_ids[i:i+batch_size]
        vs.index.delete(ids=batch, namespace=vs.namespace)

    print('Done')

asyncio.run(delete_all_legislator_votes())
```

Then rebuild:
```bash
python -m votebot.sync.build_legislator_votes
```

### Prevention

The fix in `build_legislator_votes.py` includes:
1. **UUID validation**: `is_valid_person_id()` method validates the format `ocd-person/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`
2. **Skip malformed entries**: Parser skips person IDs that don't match the valid format
3. **Defense in depth**: Double-checking in both parsing and extraction functions

---

## Missing Data in Search Results

### Symptom
Content that should be in the index isn't appearing in search results.

### Diagnostic Steps

#### 1. Verify content exists in Pinecone

```python
async def search_by_id_prefix(prefix):
    settings = get_settings()
    vs = VectorStoreService(settings)

    all_ids = []
    for ids in vs.index.list(namespace=vs.namespace, prefix=prefix):
        all_ids.extend(ids)

    print(f'Found {len(all_ids)} documents with prefix "{prefix}"')
    for id in all_ids[:10]:
        print(f'  {id}')

asyncio.run(search_by_id_prefix("bill-votes-682f"))  # HR1 example
```

#### 2. Check document metadata

```python
async def check_metadata(doc_id):
    settings = get_settings()
    vs = VectorStoreService(settings)

    result = vs.index.fetch(ids=[doc_id], namespace=vs.namespace)

    if doc_id in result.vectors:
        meta = result.vectors[doc_id].metadata
        for k, v in sorted(meta.items()):
            if k != 'content':
                print(f'{k}: {v}')
    else:
        print(f'Document not found: {doc_id}')

asyncio.run(check_metadata("bill-votes-682f4c9a5a8d551cb4777414-chunk-0"))
```

#### 3. Check overall index stats

```python
async def index_stats():
    settings = get_settings()
    vs = VectorStoreService(settings)

    stats = vs.index.describe_index_stats()
    print(f'Total vectors: {stats.total_vector_count}')

    for prefix in ['bill-', 'legislator-', 'organization-', 'web-', 'training-']:
        count = 0
        for ids in vs.index.list(namespace=vs.namespace, prefix=prefix):
            count += len(ids)
        print(f'{prefix}: {count}')

asyncio.run(index_stats())
```

### Common Causes

1. **Sync never ran**: Content wasn't synced to Pinecone
2. **Metadata filtering**: Query uses filters that exclude the document
3. **Embedding mismatch**: Content doesn't semantically match the query
4. **Document chunked**: Content is in a different chunk than expected

---

## Federal Legislator Cache Issues

### Symptom
Federal legislators' person IDs aren't being matched in bill-votes documents.

### Background
OpenStates doesn't include person IDs in federal vote records - only voter names like "Moody (R-FL)". The federal legislator cache maps these names to person IDs.

### Diagnostic Steps

#### 1. Check cache contents

```bash
python -m votebot.sync.federal_legislator_cache --show
```

#### 2. Test name lookup

```python
from src.votebot.sync.federal_legislator_cache import get_federal_cache

cache = get_federal_cache()

test_names = [
    'Moody (R-FL)',
    'Scott (R-FL)',
    'Pelosi (D-CA)',
]

for name in test_names:
    result = cache.lookup(name)
    print(f'{name!r:25} -> {result}')
```

#### 3. Check cache file

```bash
cat data/cache/federal_legislators.json | python -c "
import json, sys
data = json.load(sys.stdin)
print(f'Total legislators: {len(data.get(\"legislators\", {}))}')
print(f'Last refreshed: {data.get(\"refreshed_at\", \"unknown\")}')
"
```

### Solution

Refresh the cache from OpenStates:

```bash
python -m votebot.sync.federal_legislator_cache
```

This fetches all 538 members of Congress and builds name variant mappings.

---

## Pinecone Index Diagnostics

### Quick Health Check

```python
import asyncio
from src.votebot.config import get_settings
from src.votebot.services.vector_store import VectorStoreService

async def health_check():
    settings = get_settings()
    vs = VectorStoreService(settings)

    # Overall stats
    stats = vs.index.describe_index_stats()
    print(f'Index: {settings.pinecone_index_name}')
    print(f'Namespace: {vs.namespace}')
    print(f'Total vectors: {stats.total_vector_count}')

    # Count by document type
    prefixes = {
        'bill-webflow-': 'Bill (Webflow)',
        'bill-pdf-': 'Bill (PDF)',
        'bill-history-': 'Bill (History)',
        'bill-votes-': 'Bill (Votes)',
        'legislator-ocd-person/': 'Legislator (Profile)',
        'legislator-bills-': 'Legislator (Bills)',
        'legislator-votes-': 'Legislator (Votes)',
        'organization-': 'Organization',
        'web-': 'Webpage',
        'training-': 'Training',
    }

    print('\n=== Document Counts ===')
    for prefix, label in prefixes.items():
        count = 0
        for ids in vs.index.list(namespace=vs.namespace, prefix=prefix):
            count += len(ids)
        if count > 0:
            print(f'{label}: {count}')

asyncio.run(health_check())
```

### Test Search Quality

```python
async def test_queries():
    settings = get_settings()
    vs = VectorStoreService(settings)

    test_cases = [
        ("HR1 One Big Beautiful Bill", "bill"),
        ("Rick Scott voting record", "legislator-votes"),
        ("ACLU bill positions", "organization"),
        ("What is Digital Democracy Project", "webpage"),
    ]

    for query, expected_type in test_cases:
        results = await vs.query(query, top_k=1)
        if results:
            r = results[0]
            doc_type = r.metadata.get('document_type', 'N/A')
            match = '✓' if expected_type in r.metadata.get('document_id', '') else '✗'
            print(f'{match} "{query[:40]}..." -> {doc_type} ({r.score:.3f})')
        else:
            print(f'✗ "{query[:40]}..." -> No results')

asyncio.run(test_queries())
```

---

## Webflow CMS Verification on Disputes

### Overview

When users challenge or dispute VoteBot's answers, the system now fetches authoritative details from **Webflow CMS** for the current page entity (bill, legislator, or organization) and injects them as high-priority context before LLM generation. This supplements the existing OpenStates vote verification with CMS facts.

### How It Works

1. User sends a dispute phrase (e.g., "that's wrong", "are you sure?", "verify that")
2. `_is_dispute_or_correction()` detects the dispute trigger
3. **Webflow CMS verification** (`_verify_from_webflow()`) dispatches based on `page_context.type`:
   - **Bill page** → `get_bill_details()` → name, identifier, status, description, jurisdiction
   - **Legislator page** → `get_legislator_details()` → name, party, chamber, district, DDP score
   - **Organization page** → `get_org_details()` → name, type, website, about description
4. **OpenStates vote verification** (`_verify_legislator_vote()`) also runs — now from **any page type**, extracting bill identifier from the message text if not on a bill page
5. Both are injected as authoritative context, with Webflow CMS data first (most authoritative)

### Context Injection Order (on disputes)

```
1. Webflow CMS verification (bill/legislator/org details)     ← NEW
2. Org bill positions (if org page + bill position query)
3. Org positions (if bill page + org position query)
4. OpenStates vote verification (now from any page type)       ← UPDATED
5. Bill info / legislator info (streaming only)
6. RAG retrieval results
```

### Requirements

- `page_context` must include `webflow_id` or `slug` (provided by the chat widget via `/content/resolve`)
- `page_context.type` must be `"bill"`, `"legislator"`, or `"organization"`
- Graceful degradation: if `webflow_id`/`slug` are missing or Webflow API fails, verification is silently skipped

### Diagnostic Steps

#### 1. Check if Webflow verification was triggered

Look for these log entries:
```
"Webflow CMS verification fetched" page_type=bill webflow_id=... slug=...
"Webflow CMS verification: fetched bill details" name=... identifier=...
"Webflow CMS verification: fetched legislator details" name=... party=...
"Webflow CMS verification: fetched org details" name=...
```

If verification is not triggering:
- Confirm `is_dispute=True` in the logs
- Confirm `page_context.type` is one of `bill`, `legislator`, `organization`
- Confirm `webflow_id` or `slug` is present in `page_context`

#### 2. Test Webflow lookup manually

```python
import asyncio
from votebot.config import get_settings
from votebot.services.webflow_lookup import (
    WebflowLookupService,
    format_bill_verification_context,
    format_legislator_verification_context,
    format_org_verification_context,
)

async def test_bill_details():
    service = WebflowLookupService(get_settings())
    result = await service.get_bill_details(slug="one-big-beautiful-bill-act-hr1-2025")
    print(f"Found: {result.found}")
    print(f"Name: {result.name}")
    print(f"Identifier: {result.identifier}")
    print(f"Status: {result.status}")
    print(format_bill_verification_context(result))

async def test_legislator_details():
    service = WebflowLookupService(get_settings())
    result = await service.get_legislator_details(slug="rick-scott")
    print(f"Found: {result.found}")
    print(f"Name: {result.name}")
    print(f"Party: {result.party}")
    print(f"Chamber: {result.chamber}")
    print(format_legislator_verification_context(result))

async def test_org_details():
    service = WebflowLookupService(get_settings())
    result = await service.get_org_details(slug="aclu")
    print(f"Found: {result.found}")
    print(f"Name: {result.name}")
    print(f"Type: {result.org_type}")
    print(format_org_verification_context(result))

asyncio.run(test_bill_details())
asyncio.run(test_legislator_details())
asyncio.run(test_org_details())
```

#### 3. Test vote verification without bill page context

The vote verification now works without a bill page context by extracting the bill identifier from the message:

```python
import asyncio
from votebot.config import get_settings
from votebot.core.agent import VoteBotAgent

async def test_vote_verification_no_page():
    agent = VoteBotAgent(get_settings())
    # Simulate: user is on a legislator page but asks about a vote on HR1
    result = await agent._verify_legislator_vote(
        message="Are you sure Ashley Moody voted no on HR1?",
        page_context=None,  # No bill page context
    )
    print(result)

asyncio.run(test_vote_verification_no_page())
```

### Known Limitations

1. **Webflow ID required**: Verification only works when `webflow_id` or `slug` is in `page_context`. If the chat widget doesn't call `/content/resolve`, there's no page entity to verify.
2. **Legislator jurisdiction**: The legislator `jurisdiction` field in Webflow stores a reference ID (not a state code). The raw value is included as-is since `page_context` already provides the resolved jurisdiction.
3. **Description HTML stripping**: Bill and org descriptions are HTML-stripped and truncated to 500 characters. Complex formatting may lose structure.

---

## RAG Test Suite Diagnostics

### Overview

The RAG test suite validates response quality across all content types. Use it to measure the impact of retrieval changes and identify regression areas.

### Running a Quick Diagnostic

```bash
# Run against a random sample (no API server needed locally — Pinecone is cloud-hosted)
PYTHONPATH=src python scripts/test_rag_comprehensive.py --limit 5 --verbose

# Run specific category to isolate issues
PYTHONPATH=src python scripts/test_rag_comprehensive.py --category bills --limit 10
PYTHONPATH=src python scripts/test_rag_comprehensive.py --category organizations --limit 10
PYTHONPATH=src python scripts/test_rag_comprehensive.py --category legislators --limit 10
```

### Interpreting Results

The test suite reports:
- **Pass rate**: Percentage of validated tests that passed (tests with `passed=None` are not counted)
- **Confidence**: Average LLM confidence score across all responses
- **Citations**: Percentage of responses that included citations
- **Latency**: Average and P95 response times

### Benchmark Results (February 2026)

**Small sample (10 documents per category, 29 validated tests):**

| Category | Pass Rate | Notes |
|----------|-----------|-------|
| Bills | 91% | Title keyword matching, bill info queries |
| Legislators | 100% | With page_context (critical for scoped retrieval) |
| Organizations | 67% | Org→bill relationship queries still limited by chunk quality |
| DDP | N/A | No ground truth — confidence/citation metrics only |
| Out-of-system votes | N/A | No ground truth — tests dynamic OpenStates lookup |
| **Overall** | **86%** | Across all validated tests |

**Large sample (100 documents per category, 897 tests, 884 validated):**

| Category | Passed | Total | Rate | Notes |
|----------|--------|-------|------|-------|
| Bills | 310 | 312 | **99.4%** | After Webflow CMS bill→org lookup (was 93% before) |
| Legislators | 290 | 300 | **97%** | |
| Organizations | 290 | 292 | **99.3%** | After Webflow CMS org→bill lookup (was 96% before) |
| DDP | — | 8 | N/A | |
| Out-of-system votes | — | 5 | N/A | |
| **Overall** | **890** | **904** | **98.5%** | |

**Bill→Org template pass rate progression:**
- **58.9%** (66/112) — Before Phase 4a-i fix
- **82.1%** (92/112) — After Phase 4a-i fix (Pinecone bill chunk search)
- **99.1%** (111/112) — After Webflow CMS runtime lookup (bill→org direction)

**Org→Bill template pass rate progression:**
- **~96%** (~249/259) — Before Webflow CMS lookup (Pinecone only)
- **99.3%** (290/292) — After Webflow CMS runtime lookup (org→bill direction)
- Supported bills: **100%** (62/62), Opposed bills: **100%** (37/37)

**By jurisdiction (bills category, post Webflow CMS lookup):**

| Jurisdiction | Passed/Total | Rate |
|-------------|-------------|------|
| MA | 4/4 | 100% |
| MI | 27/27 | 100% |
| WA | 100/100 | 100% |
| VA | 68/68 | 100% |
| FL | 20/20 | 100% |
| US | 25/25 | 100% |
| AZ | 40/41 | 98% |
| UT | 26/27 | 96% |

Notable improvements after Webflow CMS lookup: US (federal) went from 64% to **100%**, WA from 96% to **100%**, VA from 96% to **100%**, FL from 95% to **100%**. All jurisdictions now ≥96%.

**Aggregate metrics (bills+legislators):** Avg confidence 0.78, avg latency 8.6s, P95 latency 18.0s, citation rate 76.5%.
**Aggregate metrics (organizations):** Avg confidence 0.79, avg latency 6.2s, P95 latency 12.9s, citation rate 73.3%.

### Failure Analysis (100-Document Sample)

#### Bills: 2 failures (down from 43 → 23 → 2 across iterative fixes)

**After Webflow CMS Lookup (current)**: 310/312 passed (99.4%). Only 2 remaining failures:

1. **UT HB 88 oppose_orgs** (1 failure): Organization lookup edge case — the single remaining bill→org failure out of 112 org-position tests
2. **AZ SB 1070 summary** (1 failure): LLM training data collision — the bill number "SB 1070" was reused, and the LLM confuses it with the famous 2010 Arizona immigration law

**Bill→Org improvement history:**
- **58.9%** (66/112) — Before Phase 4a-i (Pinecone-only retrieval)
- **82.1%** (92/112) — After Phase 4a-i (bill chunk search)
- **99.1%** (111/112) — After Webflow CMS runtime lookup (**current**)

The Webflow CMS lookup eliminated the 20 remaining Pinecone-based failures by fetching org positions directly from the authoritative CMS source, bypassing similarity threshold issues entirely.

#### Legislators: ~10 failures (unchanged — test validation issues)

Two distinct sub-issues that are **test validation problems, not retrieval problems**:

**Senator district "0" (3 failures)**: US Senators don't have numbered districts, but CMS stores district as "0". The test expects the response to contain "0", but the LLM correctly answers "senators represent the whole state." The response is accurate — the validation is wrong.

**Formal name expansion (7 failures)**: The LLM uses legislators' legal names from training data (e.g., "Robert Charles 'Chuck' Brannan III") while the test expects the CMS short name ("Chuck Brannan"). The `contains` validation fails because "Chuck Brannan" is not a substring of "Robert Charles 'Chuck' Brannan III" — the quotes around the nickname break substring matching.

Examples:
- Expected "Will Robinson" → Response has "William 'Will' Robinson Jr."
- Expected "Tyler Sirois" → Response has "Tyler I. Sirois"
- Expected "Alex Andrade" → Response has "Robert Alexander 'Alex' Andrade"
- Expected "Ralph E. Massullo, MD" → Response has "Dr. Ralph E. Massullo Jr."

**Fix needed**: Update legislator name validation to use `contains_any` with name parts (first name + last name separately) instead of exact `contains` for the full name string. Also skip district validation for senators (district "0").

#### Organizations: 2 failures (down from ~10 after Webflow CMS org→bill lookup)

**After Webflow CMS Lookup (current)**: 290/292 passed (99.3%). Only 2 remaining failures:

1. **Rural Utah Project org_type** (1 failure): Org type description mismatch — LLM doesn't produce the expected "Non-profit (501(c)(4))" keywords
2. **Humanization Project org_type** (1 failure): Same org type validation issue — "Non-profit (501(c)(3))" keywords not matched

Both failures are in org type validation, **not** in org→bill queries. All 99 org→bill tests (62 supported + 37 opposed) now pass at **100%**.

**Org→Bill improvement history:**
- **~96%** (~249/259) — Before Webflow CMS lookup (Pinecone-only retrieval)
- **99.3%** (290/292) — After Webflow CMS runtime lookup (**current**)

The Webflow CMS lookup eliminated the ~10 remaining Pinecone-based failures by fetching bill positions directly from the authoritative CMS source, bypassing chunking and similarity threshold issues entirely.

### Common Failure Patterns

1. **Org→Bill relationship queries** (RESOLVED): Previously ~10 failures from Pinecone chunking issues. Now **100%** (99/99) after Webflow CMS org→bill runtime lookup.

2. **Legislator name validation** (~7 false failures): LLM expands short names to legal names. Need `contains_any` validation with name parts.

3. **Senator district validation** (~3 false failures): CMS stores "0" for senators. Need to skip district validation for at-large representatives.

4. **Bill title exact match**: The LLM may paraphrase bill titles. The test suite uses `contains_any` with `name_keywords` (bill ID + title keywords, min 2 matches) to handle this.

5. **Org type**: The LLM may describe org types differently than CMS data. The test suite uses `contains_any` with `org_type_keywords` (min 1 match) to handle this.

### Validation Modes

| Mode | Description | Use Case |
|------|-------------|----------|
| `contains` | All expected values must appear in response | Exact data (bill ID, jurisdiction) |
| `contains_any` | At least N of the expected values must appear | Paraphrased data (titles, org types) |
| `keywords` | At least N keywords must match | Fuzzy matching |

### Adding Custom Tests

Static tests are defined in `tests/rag_test_prompts.yaml`. Dynamic tests are generated from Webflow CMS ground truth via `scripts/test_rag_quality.py`.

To add a new static test:
```yaml
- prompt: "What type of organization is the ACLU?"
  expected:
    validation_mode: contains_any
    expected_values: ["nonprofit", "civil liberties", "advocacy"]
    min_matches: 1
  category: organizations
```

---

## Full Index Rebuild Procedure

When all else fails, a complete rebuild ensures data consistency.

### Step 1: Backup current state (optional)

```python
async def export_stats():
    # Save current document counts for comparison
    # ... (run health check and save results)
    pass
```

### Step 2: Run full rebuild

```bash
# Non-interactive full rebuild
python scripts/rebuild_pinecone.py --yes

# Or step-by-step:
# 1. Wipe index
python -c "
import asyncio
from src.votebot.config import get_settings
from src.votebot.services.vector_store import VectorStoreService

async def wipe():
    settings = get_settings()
    vs = VectorStoreService(settings)
    await vs.delete(delete_all=True)
    print('Index wiped')

asyncio.run(wipe())
"

# 2. Sync all content types
python scripts/sync.py all

# 3. Build legislator-votes reverse index
python -m votebot.sync.build_legislator_votes
```

### Step 3: Verify rebuild

```bash
# Check document counts
python -c "..." # (health check script above)

# Test key queries
python -c "..." # (test queries script above)
```

### Expected Document Counts (approximate)

| Document Type | Expected Count |
|---------------|----------------|
| Bill (all types) | 20,000+ |
| Legislator (Profile) | 500-600 |
| Legislator (Bills) | 500-600 |
| Legislator (Votes) | 800-1200 |
| Organization | 1,500+ |
| Webpage | 10-20 |
| Training | 2-5 |
| **Total** | **24,000-26,000** |

---

## Vote Lookup Fails on Legislator Pages for Specific Bills

### Symptom

When on a legislator page, VoteBot cannot answer "how did he vote on HR 4405?" even when the user provides the exact bill number. VoteBot claims there is no record of the vote. But if the user navigates to the bill's page and asks the same question, VoteBot answers correctly.

### Example

```
[On Vern Buchanan's legislator page]

User: "how did he vote on the epstein files?"
Bot: "There is no record of a bill specifically titled 'the Epstein files'..."

User: "i'm talking about HR 4405 to release the epstein files"
Bot: "There is no record in the DDP database of Rep. Vern Buchanan voting on H.R. 4405..."

User: "check your sources"
Bot: "Rep. Buchanan does not have a recorded vote on H.R. 4405..."

[User navigates to the HR 4405 bill page]

User: "how did vern buchanan vote on this?"
Bot: "Rep. Vern Buchanan voted YES on HR 4405..."  ← works!
```

### Root Cause (Three Bugs)

Three separate bugs combined to prevent vote lookups from working on legislator pages:

#### Bug 1: `_prefetch_bill_info` used year instead of Congress number for federal bills

When the user mentions "HR 4405", the bill info pre-fetch correctly extracts the identifier but uses `str(datetime.now().year)` = `"2026"` as the session for the OpenStates API call. Federal bills require the Congress number (`"119"` for the 119th Congress, 2025-2027), not the calendar year.

The `_verify_legislator_vote` method already had the correct Congress number conversion (line 1146-1148), but `_prefetch_bill_info` was missing it.

```python
# OLD (broken) — _prefetch_bill_info line 864-868:
session = getattr(page_context, "session", None)
if not session:
    session = str(datetime.now().year)  # "2026" — wrong for federal bills!

# FIXED — now matches _verify_legislator_vote logic:
if not session:
    year = datetime.now().year
    if jurisdiction and jurisdiction.upper() == "US":
        congress_number = (year - 2025) // 2 + 119
        session = str(congress_number)  # "119" — correct!
    else:
        session = str(year)
```

**Impact**: Every federal bill lookup from a legislator/org page (where `page_context.session` is `None`) silently failed because OpenStates returned 404 for session "2026".

#### Bug 2: `_verify_legislator_vote` didn't check `page_context.title` for legislator name

When the dispute trigger fires on "check your sources", `_extract_legislator_name("check your sources")` returns nothing (all lowercase, no names). The method then searches conversation history:

- User messages: "how did **he** vote..." → "he" is filtered as a common word
- User messages: "i'm talking about HR 4405..." → no names, all lowercase
- Assistant messages: "**As** of now, there is no record..." → returns "As" (Bug 3)

The legislator's name is right there in `page_context.title = "Vern Buchanan"`, but it was never checked.

```python
# FIXED — added after initial message extraction:
if not legislator_name and page_context and page_context.type == "legislator" and page_context.title:
    legislator_name = page_context.title
```

#### Bug 3: `_extract_legislator_name` returned "As" from assistant messages

Method 4 (capitalized word extraction) greedily returns the first capitalized word that isn't in the `common_words` set. The word "As" (sentence-initial capitalization in "As of now, there is no record...") was not in `common_words`, so it was returned as a "legislator name" before the method ever reached "Vern Buchanan" later in the text.

```python
# FIXED — added "as" and other common sentence starters to common_words:
common_words = {
    ...,
    "as", "at", "in", "if", "it", "or", "but", "for", "with", "from",
    "has", "have", "had", "was", "were", "are", "been", "being",
    "however", "therefore", "furthermore", "additionally", "currently",
    "also", "based", "please", "note", "here", "possible", "reasons",
}
```

### Fix (February 2026)

All three bugs fixed in `src/votebot/core/agent.py`:

| Bug | Location | Fix |
|-----|----------|-----|
| Congress number | `_prefetch_bill_info` line ~865 | Added `(year - 2025) // 2 + 119` conversion for US jurisdiction |
| Legislator name from page context | `_verify_legislator_vote` line ~1067 | Fall back to `page_context.title` on legislator pages |
| "As" false positive | `_extract_legislator_name` line ~1244 | Added "as", "at", "in", "if", "it", etc. to `common_words` |

### How It Works After Fix

```
User on legislator page (Vern Buchanan): "i'm talking about HR 4405"
                                          ↓
_should_use_bill_votes_tool() → True (has_bill_identifier = "HR4405")
                                          ↓
_prefetch_bill_info() extracts:
  bill_identifier = "HR4405"
  jurisdiction = "US" (default — no jurisdiction on legislator page)
  session = "119" ← FIXED (was "2026")
                                          ↓
get_bill_info(jurisdiction="US", session="119", bill_identifier="HR4405")
                                          ↓
OpenStates returns full bill info with vote records → LLM has context → correct answer


User on legislator page: "check your sources" (dispute trigger)
                                          ↓
_verify_legislator_vote():
  legislator_name = page_context.title = "Vern Buchanan" ← FIXED (was "As")
  bill_identifier = "HR4405" (from conversation history)
  session = "119"
                                          ↓
lookup_legislator_vote("Vern Buchanan", "US", "119", "HR4405")
                                          ↓
Returns: {vote: "YES", motion: "passage", ...} → authoritative context
```

### Diagnostic Steps

#### 1. Check if bill info pre-fetch is using correct session

```bash
sudo journalctl -u votebot | grep "Pre-fetching bill info"
```

Look for:
```
"Pre-fetching bill info for streaming" jurisdiction="US" session="119" bill_identifier="HR4405"
```

If you see `session="2026"` for a US bill, the fix is not deployed.

#### 2. Check if legislator name was extracted from page context

```bash
sudo journalctl -u votebot | grep -E "legislator name|page context"
```

Look for:
```
"Using legislator name from page context" name="Vern Buchanan"
```

If instead you see `"Could not extract legislator name for vote verification"`, the page context title may be empty.

#### 3. Check if vote verification triggered and succeeded

```bash
sudo journalctl -u votebot | grep -E "dispute|verification|Verifying"
```

Look for the full chain:
```
"Checking dispute/verification trigger" is_dispute=True
"Dispute detected, attempting vote verification"
"Verifying legislator vote from OpenStates" legislator="Vern Buchanan" bill="HR4405" session="119"
"Vote verification successful"
```

### Lessons Learned

1. **Session format varies by jurisdiction**: Federal bills use Congress numbers (119, 120...), state bills use years (2025, 2026...). Any code that constructs OpenStates API calls must account for this — don't assume year format.
2. **Page context is an underused signal**: When the user says "he" or "she" on a legislator page, the name is in `page_context.title`. The system should always consider page context as a fallback for entity resolution.
3. **Capitalized word extraction is fragile**: English sentences start with capital letters. Any "extract names from capitalized words" heuristic needs an extensive stop list, or preferably a different approach (NER, explicit name patterns).
4. **Test from non-bill pages**: Vote lookups were only tested from bill pages where `page_context.session` was populated. Testing from legislator pages would have caught Bug 1.

---

## Wrong Vote Reported on Legislator Pages (LLM Ignores Data)

### Symptom

When on a legislator page and asking about a specific bill, VoteBot reports the wrong vote. The bill data IS fetched correctly from OpenStates, but the LLM misreads the data and gives an incorrect answer. When the user disputes the answer, the verification also fails.

### Example

```
[On Vern Buchanan's legislator page]

User: "i'm talking about HR 1"
Bot: "Rep. Vern Buchanan voted YES on HR 1 (One Big Beautiful Bill Act)..."
     ← WRONG: Buchanan was one of only 2 Republicans who voted NO

User: "he definitely did not vote yes. check again"
Bot: "I couldn't find specific information about Rep. Buchanan's vote on HR 1..."
     ← Verification fails silently

[User navigates to the HR 1 bill page]

User: "how did vern buchanan vote?"
Bot: "Rep. Vern Buchanan voted NO on HR 1..."  ← works correctly
```

### Root Cause (Three Bugs)

#### Bug 1: LLM drowns in voter data for large bills

The `_prefetch_bill_info` method fetches the complete bill info from OpenStates, which for HR 1 includes 435+ voter names grouped by party. The formatted document lists up to 20 names per party per vote position. For a bill where 215 Republicans voted YES and only 2 voted NO, the YES list dominates the context.

The LLM sees Buchanan is a Republican, sees the bill passed along party lines, and confidently concludes he voted YES — without carefully checking the NO list where Buchanan actually appears.

**Fix**: When on a legislator page and a bill is mentioned, `_prefetch_bill_info` now calls `find_legislator_in_votes()` using the votes already fetched from `get_bill_info` (always fresh from OpenStates). The result is prepended to the context as a `## SPECIFIC VOTE LOOKUP RESULT` section with a note that it should be used as the definitive answer.

```python
# NEW — in _prefetch_bill_info, after fetching bill info:
if page_context and page_context.type == "legislator" and page_context.title:
    # Uses votes from get_bill_info (always fresh from OpenStates)
    vote_result = self.bill_votes.find_legislator_in_votes(
        legislator_name=page_context.title,
        votes=result.votes,
        bill_identifier=result.bill_identifier,
    )
    # Prepend: "## SPECIFIC VOTE LOOKUP RESULT
    # **Vern Buchanan** voted **NO** on **HR1**
    # *This is the authoritative vote record...*"
```

#### Bug 2: `_verify_legislator_vote` used legislator ID as bill identifier

When a user disputes on a legislator page ("check again"), `_verify_legislator_vote` extracted the bill identifier from `page_context.id`:

```python
# OLD (broken):
bill_identifier = page_context.id if page_context else None
```

On a legislator page, `page_context.id` is the legislator's OpenStates ID (e.g., `ocd-person/7e5729d1-198d-5389-be51-d1e05969729c`), NOT a bill identifier. Since this string is truthy, the code **skipped** extracting the bill from the message text or conversation history. It then called `lookup_legislator_vote(..., bill_identifier="ocd-person/7e5729d1-...")` which always returned `None`.

```python
# FIXED — only use page_context.id on bill pages:
bill_identifier = None
jurisdiction = page_context.jurisdiction if page_context else None
if page_context and page_context.type == "bill":
    bill_identifier = page_context.id
```

Now on a legislator page, `bill_identifier` starts as `None`, causing the code to correctly extract "HR1" from the message text or conversation history.

#### Bug 3: Pinecone vote cache returns empty votes list (RESOLVED — cache removed)

The Pinecone vote cache in `bill_votes.py` was fundamentally lossy: `_cache_to_pinecone()` stored votes as formatted markdown text, but `_check_cache()` returned `BillVotesResult(votes=[])` — the structured `BillVote`/`VoteRecord` objects were permanently lost. Every downstream function calling `find_legislator_in_votes()` got an empty list and silently failed.

**Root cause fix (February 2026)**: The Pinecone vote cache was completely removed from `bill_votes.py`. All vote lookups now go directly to the OpenStates API:
- `_check_cache()`, `_cache_to_pinecone()`, and `format_votes_document()` were deleted
- `get_bill_votes()` now calls `_fetch_from_openstates()` directly (no cache layer)
- `lookup_legislator_vote()` now uses `get_bill_info()` (which includes session fallback for state bills) instead of the old `get_bill_votes()` → cache path
- `BillVotesResult.cached` field was removed
- Unused imports (`IngestionPipeline`, `VectorStoreService`, `DocumentMetadata`) and `__init__` attributes (`self.pipeline`, `self.vector_store`) were cleaned up

Note: The bill sync pipeline (`bill_sync.py`) still writes `document_type: bill-votes` docs to Pinecone during ingestion — that is a separate concern and remains unchanged. The RAG retrieval pipeline still queries these docs for general vote context.

### Fix (February 2026)

All three bugs fixed across two files:

| Bug | Location | Fix |
|-----|----------|-----|
| LLM ignores minority vote | `_prefetch_bill_info` in `agent.py` | Call `find_legislator_in_votes()` on legislator pages, prepend specific vote to context |
| Legislator ID used as bill ID | `_verify_legislator_vote` in `agent.py` | Only use `page_context.id` as `bill_identifier` when `page_context.type == "bill"` |
| Lossy Pinecone vote cache | `bill_votes.py` | Removed cache entirely (`_check_cache`, `_cache_to_pinecone`, `format_votes_document` deleted); `lookup_legislator_vote` now uses `get_bill_info()` (always fresh, with session fallback) |

### How It Works After Fix

```
User on legislator page (Vern Buchanan): "i'm talking about HR 1"
                                          ↓
_prefetch_bill_info():
  bill_identifier = "HR1"
  jurisdiction = "US", session = "119"
  → get_bill_info() returns full bill data (always fresh from OpenStates)
  → find_legislator_in_votes("Vern Buchanan", result.votes, "HR 1")
     (uses votes already in hand — no cache involved)
  → Returns {vote: "no", legislator: "Buchanan", motion: "On Passage"}
  → Prepends: "## SPECIFIC VOTE LOOKUP RESULT
     **Buchanan** voted **NO** on **HR1**
     *This is the authoritative vote record...*"
                                          ↓
LLM sees the specific vote result FIRST → correct answer: "Buchanan voted NO"


User: "check again" (dispute)
                                          ↓
_verify_legislator_vote():
  legislator_name = page_context.title = "Vern Buchanan"
  bill_identifier = None ← FIXED (was "ocd-person/...")
  → _extract_bill_from_text("check again") → None
  → Search conversation history → finds "HR 1" → bill_identifier = "HR1"
  → lookup_legislator_vote("Vern Buchanan", "US", "119", "HR1")
     → get_bill_info() fetches fresh from OpenStates (with session fallback)
     → find_legislator_in_votes() searches structured BillVote objects
  → Returns authoritative vote → correct verification
```

### Diagnostic Steps

#### 1. Check if specific legislator vote was looked up during pre-fetch

```bash
sudo journalctl -u votebot | grep "specific legislator vote"
```

Look for:
```
"Found specific legislator vote during bill pre-fetch" legislator="Vern Buchanan" vote="no" bill="HR1"
```

If you see `"Legislator not found in bill votes during pre-fetch"`, the name matching may have failed.

#### 2. Check if `page_context.id` is being used incorrectly

```bash
sudo journalctl -u votebot | grep "Verifying legislator vote"
```

Look for:
```
"Verifying legislator vote from OpenStates" legislator="Vern Buchanan" bill="HR1" session="119"
```

If you see `bill="ocd-person/..."`, the fix is not deployed — it's still using the legislator ID as the bill identifier.

#### 3. Verify the dispute verification finds the bill in conversation history

```bash
sudo journalctl -u votebot | grep "bill identifier"
```

Look for:
```
"Found bill identifier in conversation history" bill_identifier="HR1"
```

#### 4. Check if vote lookup is fetching from OpenStates

```bash
sudo journalctl -u votebot | grep "Fetching bill votes from OpenStates"
```

Look for:
```
"Fetching bill votes from OpenStates" jurisdiction="us" session="119" bill_identifier="HR1"
```

All vote lookups now go directly to the OpenStates API (the lossy Pinecone vote cache was removed in February 2026). If votes are not being found, check OpenStates API availability.

### Why This Only Affects Legislator Pages

On a **bill page**, all three bugs are irrelevant:
1. The bill info pre-fetch doesn't need a specific legislator vote — the user can see the bill, and the LLM has the bill context
2. `page_context.id` is already the bill identifier, and `page_context.type == "bill"` so it's used correctly

On a **legislator page**:
1. The user expects answers about THIS legislator, but the bill data contains 435 voters — the LLM has to find the needle in the haystack
2. `page_context.id` is a legislator ID, not a bill ID — it was incorrectly used for vote lookups

### Lessons Learned

1. **Don't rely on the LLM to find one name in hundreds**: For large vote events (435 House members), always do a targeted lookup and give the LLM the specific answer.
2. **`page_context.id` semantics vary by page type**: On bill pages it's a bill identifier, on legislator pages it's a legislator ID. Code that uses it must check `page_context.type` first.
3. **Minority votes are the hardest**: When 99% of a party votes one way, the LLM's training data strongly biases it toward the majority. Explicit authoritative data injection is essential.
4. **Test from ALL page types**: This bug only manifested on legislator pages. Testing only from bill pages wouldn't catch it.
5. **Don't cache structured data as text**: The Pinecone vote cache stored votes as formatted markdown but returned `votes=[]` for the structured list — the data was permanently lost on write. The fix was to remove the cache entirely and always fetch fresh from OpenStates. If caching is ever re-introduced, it must preserve the structured `BillVote`/`VoteRecord` objects.

---

## Bill Not Found When Referenced by Common Name

### Symptom

When on any page (especially legislator pages), VoteBot cannot find a bill when the user references it by its common name or title instead of its bill number. Even after a web search reveals the bill number, follow-up questions like "check how he voted on it" still fail.

### Example

```
[On Vern Buchanan's legislator page]

User: "how did he vote on one big beautiful bill act?"
Bot: "There is no record of a bill titled 'One Big Beautiful Bill Act'..."
     ← Bill exists as HR 1 but system can't resolve the title

User: "do a web search"
Bot: [Web search finds bill info, mentions H.R. 1, but no vote lookup happens]

User: "check to see how he voted on it"
Bot: "Currently, there is no official record..."
     ← Still fails because "it" has no bill number to extract
```

### Root Cause

The bill pre-fetch system (`_prefetch_bill_info`) only worked when the user's message contained an explicit bill number pattern (HR 1, HB 123, SB 456, etc.). It used a regex to extract the identifier:

```python
bill_pattern = r'\b(hb|sb|hr|s|hj|sj|hcr|scr|hjr|sjr)\s*(\d+)'
match = re.search(bill_pattern, message_lower)
if not match:
    return ""  # ← Immediately gave up if no bill number pattern found
```

When the user said "one big beautiful bill act", no regex matched, so no bill info was fetched. The LLM had no bill data to work with and couldn't answer.

Additionally, `_prefetch_bill_info` had no access to conversation history. Even when a previous web search response contained "H.R. 1", follow-up messages like "check how he voted on it" couldn't find it.

### Fix (February 2026)

Added three-tier bill identifier resolution to `_prefetch_bill_info`:

**Method 1 — Regex extraction (existing):** Matches explicit bill numbers like "HR 1", "HB 123"

**Method 2 — Pinecone title search (NEW):** When no regex match, searches Pinecone with `document_type="bill"` filter using the message as a semantic query. Extracts `bill_prefix` + `bill_number` from the top result's metadata if the similarity score exceeds 0.7.

```python
async def _resolve_bill_from_title(self, message: str) -> tuple[str | None, str | None]:
    # Guard: only search if message contains bill-related terms
    bill_terms = ["bill", "act", "resolution", "legislation", "law"]
    if not any(term in message.lower() for term in bill_terms):
        return None, None

    results = await self.bill_votes.vector_store.query(
        query=message, top_k=1, filter={"document_type": "bill"}
    )
    if results and results[0].score > 0.7:
        metadata = results[0].metadata
        bill_prefix = metadata.get("bill_prefix", "")
        bill_number = metadata.get("bill_number", "")
        if bill_prefix and bill_number:
            return f"{bill_prefix}{bill_number}", metadata.get("jurisdiction")
    return None, None
```

**Method 3 — Conversation history search (NEW):** When methods 1 and 2 fail, searches the last 6 messages in conversation history for bill identifier patterns using `_extract_bill_from_text()`. This handles follow-up questions like "check how he voted on it" after a web search revealed "H.R. 1".

The same three-tier resolution was also added to `_verify_legislator_vote` for dispute scenarios.

### Files Changed

| File | Change |
|------|--------|
| `src/votebot/core/agent.py` | NEW: `_resolve_bill_from_title()` method; MODIFIED: `_prefetch_bill_info()` now accepts `conversation_history` and uses three-tier resolution; MODIFIED: `_verify_legislator_vote()` also uses Pinecone title fallback |

### How It Works After Fix

```
User on legislator page: "how did he vote on one big beautiful bill act?"
                                          ↓
_prefetch_bill_info():
  Method 1: regex("one big beautiful bill act") → no match
  Method 2: Pinecone search("one big beautiful bill act", document_type="bill")
            → top result: HR 1 (score 0.92), bill_prefix="HR", bill_number="1"
            → bill_identifier = "HR1", jurisdiction = "US"
  → get_bill_info("US", "119", "HR1") → full bill data
  → lookup_legislator_vote("Vern Buchanan", ...) → specific vote
  → LLM has complete context → correct answer


User: "check to see how he voted on it"
                                          ↓
_prefetch_bill_info():
  Method 1: regex("check to see how he voted on it") → no match
  Method 2: Pinecone("check to see how he voted on it") → no bill terms → skip
  Method 3: conversation history search:
            → Previous bot response contains "HR 1" → extracted!
            → bill_identifier = "HR1"
  → get_bill_info("US", "119", "HR1") → correct answer
```

### Diagnostic Steps

#### 1. Check if Pinecone title resolution triggered

```bash
sudo journalctl -u votebot | grep "Resolved bill from title"
```

Look for:
```
"Resolved bill from title via Pinecone" query="how did he vote on one big beautifu..." bill_identifier="HR1" jurisdiction="US" score=0.92
```

#### 2. Check if conversation history search found a bill

```bash
sudo journalctl -u votebot | grep "conversation history for pre-fetch"
```

Look for:
```
"Found bill identifier in conversation history for pre-fetch" bill_identifier="HR1"
```

#### 3. Verify bills have required metadata in Pinecone

The `_resolve_bill_from_title` method depends on `bill_prefix` and `bill_number` metadata fields being populated on bill documents. These come from the `extra` dict in `WebflowSource._process_bill_item()`. If a bill was ingested without these fields, title resolution won't work.

```python
import asyncio
from votebot.config import get_settings
from votebot.services.vector_store import VectorStoreService

async def check_bill_metadata(title_query):
    vs = VectorStoreService(get_settings())
    results = await vs.query(title_query, top_k=1, filter={"document_type": "bill"})
    if results:
        m = results[0].metadata
        print(f"Score: {results[0].score:.3f}")
        print(f"bill_prefix: {m.get('bill_prefix')}")
        print(f"bill_number: {m.get('bill_number')}")
        print(f"jurisdiction: {m.get('jurisdiction')}")
        print(f"title: {m.get('title')}")
    else:
        print("No results found")

asyncio.run(check_bill_metadata("one big beautiful bill act"))
```

### Lessons Learned

1. **Users don't always know bill numbers**: The most common way people reference legislation is by its popular name ("one big beautiful bill act", "the epstein files"). Bill number regex is necessary but insufficient.
2. **Pinecone is already a bill title index**: Every bill document has the title embedded in its content and metadata. A simple semantic search with `document_type="bill"` filter resolves most title-to-number lookups.
3. **Conversation history is context**: When a user says "check how he voted on it", the "it" refers to a previous message. Without conversation history access, every message is treated in isolation.
4. **Three-tier fallback is robust**: Regex (fast, precise) → Pinecone (semantic, handles titles) → History (handles pronouns/references). Each tier catches what the previous one misses.

---

## Vote Lookup Fails on Bill Pages for Specific Legislators

### Symptom

When on a **bill page**, VoteBot cannot correctly answer "how did Ashley Moody vote on this?" The LLM either hallucates from training data (e.g., "Ashley Moody is the Attorney General of Florida..."), gives a wrong vote direction, or says there is no record — even though the bill has 435+ recorded votes in OpenStates. Follow-up questions using pronouns ("how did she vote on final passage?") also fail.

### Example

```
[On HR 1 bill page (One Big Beautiful Bill Act)]

User: "how did ashley moody vote on this?"
Bot: "Ashley Moody is the Attorney General of Florida, not a member of Congress..."
     ← Wrong. She's a US Representative who voted on this bill.

User: "check your sources"
Bot: "Rep. Moody voted YES on HR 1"
     ← Dispute verification works (it already used page_context.id),
        but the initial answer was wrong.

User: "how did she vote on final passage?"
Bot: "I don't have a specific vote record..."
     ← Pronoun "she" not resolved to "Ashley Moody" from history.
```

### Root Cause (Three Bugs)

Three bugs in `_prefetch_bill_info` combined to break vote lookups on bill pages:

#### Bug 1: `_prefetch_bill_info` never used `page_context.id` as bill identifier fallback

When the user says "this" or "the bill" (no explicit bill number), the three extraction methods all fail:

- Method 1 (regex): "how did ashley moody vote on this?" → no bill number pattern
- Method 2 (Pinecone title search): not a bill title → no match
- Method 3 (conversation history): first message, history is empty

The function returns `""` immediately, so **no OpenStates fetch happens**. The `page_context.id = "HR 1"` was available but never checked.

```python
# OLD (broken):
# Method 3: conversation history search
if not bill_identifier and conversation_history:
    ...

if not bill_identifier:
    return ""  # ← Gave up here. page_context.id never checked.
```

#### Bug 2: Targeted legislator vote lookup only ran on legislator pages

Even when bill info IS fetched (e.g., user says "how did Moody vote on HR 1?" with the explicit bill number), the `find_legislator_in_votes()` call was gated behind `page_context.type == "legislator"`:

```python
# OLD (broken):
if page_context and page_context.type == "legislator" and page_context.title:
    vote_result = self.bill_votes.find_legislator_in_votes(...)
```

On bill pages (`page_context.type == "bill"`), this block was skipped entirely. The LLM received 435+ voter names in the raw bill document with no targeted result, leading to wrong answers or hallucinations.

#### Bug 3: Pronoun references ("she", "he") not resolved from conversation history

When the user says "how did she vote on final passage?", `_extract_legislator_name` filters out "she" as a common word and returns `None`. Without a legislator name, no targeted vote lookup runs. There was no fallback to check conversation history for the name from previous messages.

### Fix (February 2026)

#### Fix 1: Added `page_context.id` as Method 4 fallback

After Method 3 (conversation history) and before giving up, check `page_context.id` when on a bill page:

```python
# Method 4: Fall back to page_context.id when on a bill page
# This handles "how did X vote on this?" where "this" refers to the current bill
if not bill_identifier and page_context and page_context.type == "bill" and page_context.id:
    bill_identifier = page_context.id
```

This ensures OpenStates bill info (with structured votes) is always fetched on bill pages.

#### Fix 2: Generalized targeted legislator vote lookup to all page types

Replaced the `page_context.type == "legislator"` gate with logic that works on any page:

```python
legislator_name = None

if page_context and page_context.type == "legislator" and page_context.title:
    # Legislator pages: name is in page context (existing behavior)
    legislator_name = page_context.title
else:
    # Other page types: extract name from message text
    legislator_name = self._extract_legislator_name(message)
    # Fall back to conversation history for pronoun resolution
    if not legislator_name and conversation_history:
        for msg in reversed(conversation_history[-6:]):
            if msg.get("role") == "user":
                legislator_name = self._extract_legislator_name(msg.get("content", ""))
                if legislator_name:
                    break

if legislator_name and result.votes:
    vote_result = self.bill_votes.find_legislator_in_votes(...)
```

The conversation history fallback resolves "she" → "Ashley Moody" by finding the name in earlier user messages.

### Files Changed

| File | Change |
|------|--------|
| `src/votebot/core/agent.py` | MODIFIED: `_prefetch_bill_info()` — added Method 4 (`page_context.id` fallback); generalized legislator vote lookup to all page types with conversation history pronoun resolution |

### How It Works After Fix

```
User on bill page (HR1): "how did ashley moody vote on this?"
                                          ↓
_prefetch_bill_info():
  Method 1: regex → no bill number in message → None
  Method 2: Pinecone title → not a title → None
  Method 3: conversation history → empty → None
  Method 4 (NEW): page_context.id → "HR 1" ✓
  → get_bill_info("US", "119", "HR 1") → fetches from OpenStates
  → _extract_legislator_name("how did ashley moody vote on this?") → "Ashley Moody"
  → find_legislator_in_votes("Ashley Moody", result.votes, "HR 1")
  → Returns {vote: "yes", legislator: "Moody", motion: "On Passage"}
  → Prepends: "## SPECIFIC VOTE LOOKUP RESULT
     **Moody** voted **YES** on **HR 1**
     *This is the authoritative vote record...*"
                                          ↓
LLM sees the specific vote result FIRST → correct answer


User: "how did she vote on final passage?"
                                          ↓
_prefetch_bill_info():
  Methods 1-3 → None
  Method 4: page_context.id → "HR 1" ✓
  → get_bill_info() fetches from OpenStates
  → _extract_legislator_name("how did she vote...") → None ("she" filtered)
  → conversation history search → finds "ashley moody" in prior message → "Ashley Moody"
  → find_legislator_in_votes("Ashley Moody", result.votes, "HR 1")
  → Prepends specific vote result → correct answer
```

### Diagnostic Steps

#### 1. Check if page_context.id fallback triggered

```bash
sudo journalctl -u votebot | grep "page_context.id as bill identifier"
```

Look for:
```
"Using page_context.id as bill identifier for pre-fetch" bill_identifier="HR 1"
```

#### 2. Check if legislator vote lookup found a match on bill pages

```bash
sudo journalctl -u votebot | grep "specific legislator vote during bill pre-fetch"
```

Look for:
```
"Found specific legislator vote during bill pre-fetch" legislator="Ashley Moody" vote="yes" bill="HR 1"
```

#### 3. If pronoun resolution failed, check conversation history

If the user uses "she"/"he" and the lookup fails, verify that there was a prior message with the legislator's name:

```bash
sudo journalctl -u votebot | grep "Legislator not found in bill votes"
```

If `legislator=None` is logged, the name extraction failed entirely — neither the message nor conversation history had a recognizable name.

### Lessons Learned

1. **`page_context` is always available on bill pages**: The `page_context.id` contains the bill identifier — it should always be used as a last resort before giving up on bill identification.
2. **Targeted vote lookups aren't just for legislator pages**: When a user asks about a specific legislator on ANY page type, the LLM can't reliably find one name among 435 voters. Targeted extraction should always run.
3. **Pronouns need conversation history**: Users naturally say "she" or "he" in follow-up questions. Without searching conversation history for the antecedent name, every pronoun reference breaks the pipeline.
4. **Dispute verification already worked**: `_verify_legislator_vote` already used `page_context.id` for bill pages (Bug 1 only affected `_prefetch_bill_info`). This caused the confusing behavior where "check your sources" gave the right answer but the initial response didn't.

---

## Wrong Organization Returned on Org Pages

### Symptom

When a user is on an organization page (e.g., VFW — Veterans of Foreign Wars) and asks a generic question like "tell me about this org", VoteBot returns information about a **completely different organization** (e.g., Virginia Organizing). This happens because organization retrieval had no page-context awareness — unlike bills and legislators, which filter by `webflow_id`/`slug`, org pages did unscoped semantic search across ALL organizations.

### Example

```
User navigates to: digitaldemocracyproject.org/organizations/veterans-of-foreign-wars
User: "tell me about this org"
VoteBot: "Virginia Organizing is a nonprofit that..." ← WRONG
```

### Root Cause (Four-Layer Problem)

This bug had four contributing causes, all of which had to be fixed:

#### Layer 1: No organization case in `_build_filters()` (retrieval.py)

`_build_filters()` handled `bill` and `legislator` page types but had **no `organization` case**. When `page_context.type == "organization"`, it returned empty filters `{}`, so no Pinecone scoping was applied.

#### Layer 2: Retrieval routing based on query, not page context (retrieval.py)

`retrieve()` routed to `_retrieve_organization_priority()` based on **query content** (`_is_organization_query(query)`) rather than **page context** (`page_context.type == "organization"`). A generic query like "tell me about this org" didn't match the org keyword patterns, so it fell through to the default unscoped search.

#### Layer 3: `_retrieve_organization_priority()` ignored filters (retrieval.py)

Phase 1 of `_retrieve_organization_priority()` searched with `{"document_type": "organization"}` only, ignoring the `filters` parameter entirely. Even if filters had been populated (which they weren't due to Layer 1), they wouldn't have been used.

#### Layer 4: No `ORGANIZATION_CONTEXT_PROMPT` in prompts.py

Organization pages fell through to `GENERAL_CONTEXT_PROMPT` in `build_system_prompt()`. The LLM never received "you are discussing VFW" instructions, so even if RAG pulled the right docs, there was no system prompt anchor for the correct organization.

### Fix (February 2026)

**Fix 1 — Organization filter case (`retrieval.py` `_build_filters()`):**

Added `organization` case mirroring the bill/legislator pattern:

```python
elif page_context.type == "organization":
    if page_context.webflow_id:
        filters["webflow_id"] = page_context.webflow_id
    elif page_context.slug:
        filters["slug"] = page_context.slug
```

**Fix 2 — Context-based routing (`retrieval.py` `retrieve()`):**

Changed routing to trigger on page context OR query content:

```python
# OLD: elif self._is_organization_query(query):
# NEW:
elif effective_context.type == "organization" or self._is_organization_query(query):
```

This ensures org pages ALWAYS route to org-priority retrieval, regardless of query wording. The existing query-based check still works for org queries on non-org pages.

**Fix 3 — Use filters in Phase 1 (`retrieval.py` `_retrieve_organization_priority()`):**

Merged page-context filters with `document_type` in Phase 1:

```python
org_filter = {"document_type": "organization"}
if filters.get("webflow_id"):
    org_filter["webflow_id"] = filters["webflow_id"]
elif filters.get("slug"):
    org_filter["slug"] = filters["slug"]
```

When on an org page, Phase 1 is scoped to the current organization's chunks only. When no filters exist (e.g., org query on a general page), it falls back to the existing behavior.

**Fix 4 — Organization context prompt (`prompts.py`):**

Added `ORGANIZATION_CONTEXT_PROMPT` with org-specific focus areas (mission, bill positions, policy areas) and `_format_org_info()` helper. Updated `build_system_prompt()` to route `page_type == "organization"` to the new prompt instead of falling through to `GENERAL_CONTEXT_PROMPT`.

### Resolution Flow

```
Webflow page → {type: "organization", webflow_id: "abc123", slug: "veterans-of-foreign-wars", title: "VFW"}
                                    ↓
              _build_filters(page_context) → {"webflow_id": "abc123"}
              effective_context.type == "organization" → route to org priority
                                    ↓
              _retrieve_organization_priority():
                Phase 1: filter={"document_type": "organization", "webflow_id": "abc123"}
                         → returns VFW chunks only (not Virginia Organizing)
                Phase 2: fetch all VFW chunks by document_id
                Phase 3: fill remaining slots with general results
                                    ↓
              build_system_prompt(page_type="organization", page_info={name: "VFW", ...})
                → ORGANIZATION_CONTEXT_PROMPT: "The user is viewing VFW..."
                                    ↓
              LLM knows it's discussing VFW → correct answer
```

### Files Changed

| File | Change |
|------|--------|
| `src/votebot/core/retrieval.py` | Added `organization` case in `_build_filters()`, context-based routing in `retrieve()`, filter merging in `_retrieve_organization_priority()` |
| `src/votebot/core/prompts.py` | Added `ORGANIZATION_CONTEXT_PROMPT`, `_format_org_info()`, org routing in `build_system_prompt()` |

### Diagnostic Steps

#### 1. Check if filters are populated for org pages

```bash
sudo journalctl -u votebot --since "5 minutes ago" | grep "Built retrieval filters"
```

Look for:
```
"Built retrieval filters" page_type=organization filters={"webflow_id": "abc123"}
```

If you see `filters={}` for an organization page, the `_build_filters()` org case is not working.

#### 2. Check if org-priority retrieval is triggered

```bash
sudo journalctl -u votebot --since "5 minutes ago" | grep "Organization retrieval phase"
```

Look for:
```
"Organization retrieval phase 1" org_chunks_found=N
```

If you see the default "Retrieval completed" without org phase logs, the routing condition isn't matching.

#### 3. Verify the Webflow template sends org context

The Webflow organization CMS template custom code should include:
```javascript
window.DDPChatConfig = window.DDPChatConfig || {};
window.DDPChatConfig.pageContext = {
    type: 'organization',
    title: '{{wf {"path":"name","type":"PlainText"} }}',
    slug: '{{wf {"path":"slug","type":"PlainText"} }}'
};
```

### Comparison: Bill vs Legislator vs Organization Filter Chains

| Aspect | Bills | Legislators | Organizations |
|--------|-------|-------------|---------------|
| Primary filter | `webflow_id` | `legislator_id` (OpenStates ID) | `webflow_id` |
| Fallback 1 | `slug` | `webflow_id` | `slug` |
| Fallback 2 | — | `slug` | — |
| Pre-retrieval resolution | `_lookup_bill_slug()` (from query) | `_resolve_legislator_id()` (from Webflow CMS) | — (direct filter) |
| Context prompt | `BILL_CONTEXT_PROMPT` | `LEGISLATOR_CONTEXT_PROMPT` | `ORGANIZATION_CONTEXT_PROMPT` |

### Verified (February 7, 2026)

Tested live on the VFW organization page (`digitaldemocracyproject.org/organizations/veterans-of-foreign-wars`). All four query types passed:

| Query | Before Fix | After Fix | Status |
|-------|-----------|-----------|--------|
| "tell me about this org" | Returned Virginia Organizing | Returned VFW — mission, type, focus areas, legislative engagement | PASS |
| "which bills have they supported?" | Inconsistent — could return wrong org's bills | Returned HR 980 (Veterans Readiness and Employment Improvement Act) and HR 3123 (Ernest Peltz Accrued Veterans Benefits Act) from Webflow CMS | PASS |
| "have they supported other bills not on this list?" | N/A | Stayed grounded — cited only CMS data, did not hallucinate additional bills | PASS |
| "do a web search" | Would search for wrong org | Web search correctly anchored on VFW — returned PACT Act, Post-9/11 GI Bill, VET Act, and Georgia HB 108 (opposed) | PASS |

Key observations:
- **Welcome message** correctly identified VFW on page load (system prompt anchoring working)
- **Webflow CMS pre-fetch** returned authoritative bill positions (HR 980 + HR 3123) — same as the CMS data
- **Web search fallback** searched for the correct organization because `ORGANIZATION_CONTEXT_PROMPT` anchored the LLM on "Veterans of Foreign Wars"
- **Citations** linked back to the DDP profile page

### Lessons Learned

1. **Symmetry across page types**: When adding page-context awareness for one entity type, all three layers must be updated: `_build_filters()`, `retrieve()` routing, and `build_system_prompt()`. Missing any one layer can cause wrong results.
2. **Query-based routing is insufficient**: Generic questions like "tell me about this" don't trigger keyword-based detection. Page context must be the primary routing signal.
3. **Retrieval functions must respect filters**: Adding filters to `_build_filters()` is useless if the downstream retrieval function ignores the `filters` parameter.

### Prevention

- When adding a new page context type, use the following checklist:
  1. `_build_filters()` in `retrieval.py` — add filter case
  2. `retrieve()` in `retrieval.py` — add context-based routing
  3. Retrieval function — use filters in Pinecone queries
  4. `build_system_prompt()` in `prompts.py` — add context prompt
  5. `_format_*_info()` in `prompts.py` — add format helper

---

## Sync Task Status Returns 404 in Multi-Worker Deployment

### Symptom

User triggers a batch sync via `POST /votebot/v1/sync/unified` and receives a `task_id`. When polling `GET /votebot/v1/sync/unified/status/{task_id}`, the response is `404 Task not found` instead of the expected status.

### Root Cause

Background sync task state was stored in a **per-process in-memory dict** (`_background_tasks`). With 2 uvicorn workers:

1. **Worker A** handles the POST request → creates `task_id` → stores it in Worker A's `_background_tasks` → starts background `asyncio.create_task()`
2. **Worker B** receives the GET status request → looks up `_background_tasks[task_id]` → not found (Worker B has its own empty dict) → returns 404

This is the same class of bug as [Human Handoff Messages Dropped in Multi-Worker Deployment](#human-handoff-messages-dropped-in-multi-worker-deployment) — per-process state invisible across workers.

### Solution

**Write-through to Redis** with in-memory fast-path (commit: see git log).

#### Changes to `services/redis_store.py`

Added two methods to `RedisStore`:

- `set_sync_task(task_id, task_data)` — stores task JSON at `votebot:sync:task:{task_id}` with 24-hour TTL
- `get_sync_task(task_id)` — retrieves and deserializes task data; returns `None` if missing or Redis unavailable

#### Changes to `api/routes/sync_unified.py`

1. **POST handler** — after creating `_background_tasks[task_id]`, also writes to Redis
2. **Background task** — writes to Redis at every state transition: `accepted` → `running` → `completed`/`failed`
3. **GET handler** — checks `_background_tasks` first (same-worker fast path), falls back to Redis (cross-worker), then 404

#### Graceful degradation

If Redis is unavailable, `set_sync_task()` no-ops and `get_sync_task()` returns `None`. Same-worker requests still work via the in-memory dict — behavior is identical to the pre-fix code.

### Verification

```bash
# 1. Trigger batch sync
curl -X POST -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"content_type":"training","mode":"batch"}' \
  https://api.digitaldemocracyproject.org/votebot/v1/sync/unified

# 2. Poll status (should work even if routed to different worker)
curl -H "Authorization: Bearer $API_KEY" \
  https://api.digitaldemocracyproject.org/votebot/v1/sync/unified/status/{task_id}

# 3. Verify Redis storage
redis-cli GET votebot:sync:task:{task_id}

# 4. Verify 24-hour TTL
redis-cli TTL votebot:sync:task:{task_id}
```

### Lessons Learned

1. **Any per-process state is a multi-worker bug waiting to happen.** When adding in-memory dicts for cross-request state, always ask: "Will this work if the next request hits a different worker?"
2. **Write-through with in-memory fast-path** is the right pattern when Redis is available but not guaranteed — same-worker reads are fast, cross-worker reads degrade gracefully, and the code works identically without Redis (single-worker mode).

---

## Chat Widget Truncated on Mobile (Send Button Cut Off)

### Symptom

On mobile devices, the chat widget popup opens larger than the visible screen — truncated on the right (send button cut off) and/or at the bottom (input area hidden behind the address bar). This only occurs when the widget is embedded on content-rich host pages (e.g., the DDP homepage at `digitaldemocracyproject.org`), not on the standalone test site (`votebot.digitaldemocracyproject.org`). Pinching to zoom out on the host page makes the widget "resettle" to fill the screen correctly, suggesting a viewport zoom/scale issue.

### Example

```
Mobile browser:
       ┌─ Visible viewport ─┐
       │ VoteBot Header    X │
       │                     │  ← Right edge of popup extends
       │ Chat messages...    │     beyond visible viewport
       │                     │
       │ [Input area] [Se    │  ← Send button cut off
       └─────────────────────┘
                              ← Bottom also clipped by address bar
```

### Root Cause

**The Webflow host page expands the mobile layout viewport beyond 480px.**

On `digitaldemocracyproject.org`, content (e.g., embeds with `width="940"`, wide tables, or Webflow layout elements) causes the browser's **layout viewport** to expand beyond the physical screen width on mobile devices. This has two cascading effects:

1. **CSS media queries fail**: `@media (max-width: 480px)` doesn't match because `window.innerWidth` reports the expanded layout viewport (e.g., 940px), not the physical screen width (e.g., 390px). The mobile styles never activate.
2. **JavaScript viewport checks fail**: `window.innerWidth <= 480` also evaluates to `false` for the same reason. Any JS fix relying on viewport-based detection also fails.

With the mobile styles not applying, the **desktop styles** take effect: `width: 400px; right: 24px; bottom: 100px;`. A 400px-wide popup positioned 24px from the right overflows on a 390px physical screen, cutting off the send button.

```
Physical screen: 390px wide
Layout viewport: 940px wide (expanded by page content)

CSS @media (max-width: 480px) → FALSE (940 > 480)
JS window.innerWidth <= 480   → FALSE (940 > 480)

Result: Desktop styles apply → 400px popup at right: 24px → overflows physical screen
```

### Why the Test Site Wasn't Affected

The standalone test site at `votebot.digitaldemocracyproject.org` (`chat-widget/index.html`) is a simple HTML page with:
- `maximum-scale=1.0` in the viewport meta tag (prevents layout viewport expansion)
- `* { margin: 0; padding: 0; box-sizing: border-box; }` (no content overflow)
- No embeds, wide elements, or Webflow framework

On this page, `window.innerWidth` correctly reports the physical screen width, CSS media queries match, and mobile styles activate.

### Fix (February 2026) — VERIFIED

**Viewport meta reset: force device-width while popup is open**

The fix addresses the root cause — the expanded layout viewport — by temporarily resetting the viewport meta tag when the popup opens on mobile. Since the popup is full-screen, the user doesn't see the underlying page reflow.

**Why this works**: The test site (`votebot.digitaldemocracyproject.org`) has `maximum-scale=1.0` in its viewport meta, which prevents the browser from expanding the layout viewport. The DDP production site does not. By temporarily adding `maximum-scale=1` when the popup opens, we get the same behavior.

**1. Reset viewport meta when popup opens**

```javascript
var _savedViewportContent = null;

function fixMobileSize() {
    if (!elements.chatPopup) return;
    var isMobile = screen.width <= 480 || screen.height <= 480;
    if (isMobile) {
        // Force layout viewport to device-width
        var meta = document.querySelector('meta[name="viewport"]');
        if (meta) {
            _savedViewportContent = meta.getAttribute('content');
            meta.setAttribute('content',
                'width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no');
        }
        // Set full-screen inline styles — after viewport reset,
        // 100vw/100vh correctly map to device-width
        // Use window.innerHeight for height (not 100vh which includes
        // the address bar, and not visualViewport.height which shrinks
        // when the keyboard opens). After the viewport meta reset,
        // innerHeight gives the correct full visible height.
        var h = window.innerHeight;

        var s = elements.chatPopup.style;
        s.position = 'fixed';
        s.top = '0'; s.left = '0'; s.right = '0'; s.bottom = 'auto';
        s.width = '100vw'; s.height = h + 'px';
        s.maxWidth = 'none'; s.maxHeight = 'none';
        s.borderRadius = '0';
    } else {
        // Desktop — clear all inline overrides
    }
}
```

**2. Restore viewport meta when popup closes**

```javascript
function restoreViewport() {
    if (_savedViewportContent !== null) {
        var meta = document.querySelector('meta[name="viewport"]');
        if (meta) meta.setAttribute('content', _savedViewportContent);
        _savedViewportContent = null;
    }
}
```

Called from both `closePopup()` and `togglePopup()`.

**3. Keyboard behavior**

The popup intentionally does **NOT** listen for `visualViewport.resize`. That event fires when the on-screen keyboard opens, which would shrink the popup to the tiny area above the keyboard. Instead, the popup stays full-height and the browser's default behavior scrolls the focused input into view above the keyboard.

**4. CSS `inset: 0` with `width: auto` (baseline for well-behaved pages)**

```css
@media (max-width: 480px) {
    .ddp-chat-popup {
        position: fixed;
        top: 0; left: 0; right: 0; bottom: 0;
        width: auto; height: auto;
        max-width: none; max-height: none;
    }
}
```

- `screen.width` detects mobile (unaffected by layout viewport expansion)
- Viewport meta reset collapses the layout viewport to device-width, making `100vw` and CSS media queries work correctly
- Height uses `window.innerHeight` pixels (not `100vh`, which includes the address bar on mobile; not `visualViewport.height`, which shrinks when the keyboard opens)
- `bottom: auto` avoids over-constraining the box when an explicit height is set
- Popup does NOT resize when the keyboard opens — stays full-height, browser scrolls the input into view
- Original viewport meta is saved and restored when the popup closes
- Fires on popup open, window resize, and orientation change

### What Didn't Work

| # | Approach | Problem |
|---|----------|---------|
| 1 | CSS `width: 100%` | Resolves to ancestor width if any ancestor has `transform`; also, if media query doesn't match (layout viewport > 480px), this rule never activates |
| 2 | CSS `width: 100vw` | Can include scrollbar width; reflects expanded layout viewport, not physical screen |
| 3 | CSS `width: auto` + `inset: 0` | Still derived from the CSS containing block; media query also fails to match |
| 4 | JS `window.innerWidth <= 480` check + `clientWidth` | `window.innerWidth` reports the **expanded layout viewport** (e.g., 940px on a 390px phone), so the mobile detection check fails and JS fix never activates |
| 5 | JS `screen.width` detection + pixel width from `screen.width` | Mobile detection works, but `position: fixed` resolves against the expanded layout viewport — pixel dimensions don't map to the physical screen |
| 6 | JS `setAttribute('style', ...)` with `width: 100% !important` | `100%` of the layout viewport = wider than the physical screen. Also broke the close button because `display: flex !important` overrode the CSS `display: none` when closing |
| 7 | JS `overflow-x: hidden` on `<html>` + Visual Viewport API | `overflow-x: hidden` clips content but doesn't collapse an already-expanded layout viewport. The viewport remains wide even with overflow hidden |
| 8 | Height: `100vh` | On mobile, `100vh` includes the browser address bar/toolbar height, clipping the bottom of the popup |
| 9 | Height: `visualViewport.height` + resize listener | Correct height initially, but `visualViewport.resize` fires when the on-screen keyboard opens, shrinking the popup to a tiny sliver above the keyboard |

### Files Changed

| File | Change |
|------|--------|
| `chat-widget/src/styles.css` | Mobile: `width: auto; height: auto; max-width: none; max-height: none` with `inset: 0` |
| `chat-widget/src/ui.js` | Added `fixMobileSize()` + `restoreViewport()` — uses `screen.width` for mobile detection, resets viewport meta to force device-width, sets `100vw` width + `window.innerHeight` px height. Intentionally does NOT resize on keyboard open. Restores original viewport meta on close |
| `chat-widget/dist/ddp-chat.min.js` | Rebuilt with CSS + JS changes |

### Verification

Test on a mobile device (or Chrome DevTools mobile emulator):

1. Open `digitaldemocracyproject.org` on a mobile device
2. Tap the chat widget button
3. The popup should fill exactly the visible viewport — all edges flush with the screen
4. Verify the send button is fully visible on the right (not clipped)
5. Verify the input area is fully visible at the bottom (not clipped by address bar)
6. Type a message and verify the send button is tappable
7. Close the popup with the X button — verify the underlying page returns to its original zoom/layout
8. Tap the input to open the keyboard — popup stays full-height, input scrolls into view above the keyboard

### Deployment

After the fix, the rebuilt `ddp-chat.min.js` must be deployed:

```bash
# On the server
scp chat-widget/dist/ddp-chat.min.js your-server:/var/www/votebot/

# If the Webflow site loads the script from the API server:
# The static file serving in nginx will pick up the new file automatically
```

For Webflow-hosted sites that load the widget via a `<script>` tag, ensure the script URL points to the updated file. Browser caching may delay propagation — consider adding a cache-busting query parameter if needed.

### Diagnostic: Checking for Ancestor Transforms

If the widget is still mispositioned on a specific site, check if any ancestor has transform-related CSS:

```javascript
// Run in browser console on the host page
let el = document.getElementById('ddp-chat-widget');
while (el) {
    const style = getComputedStyle(el);
    const props = ['transform', 'willChange', 'perspective', 'filter', 'contain'];
    props.forEach(p => {
        const v = style[p];
        if (v && v !== 'none' && v !== 'auto' && v !== 'normal') {
            console.log(`${el.tagName}.${el.className}: ${p} = ${v}`);
        }
    });
    el = el.parentElement;
}
```

If any ancestor reports a non-default value, that's the element breaking `position: fixed`.

### Lessons Learned

1. **Layout viewport expansion breaks everything**: On pages with wide content (embeds, iframes, tables), the mobile browser's layout viewport can expand beyond the physical screen. This breaks CSS media queries, `window.innerWidth`, `100vw`, `100%`, `position: fixed` dimensions, and even `overflow-x: hidden` can't collapse it after the fact. The viewport meta tag is the only thing that controls layout viewport size.
2. **Fix the root cause: reset the viewport meta**: Instead of fighting the expanded layout viewport with CSS/JS overrides, temporarily reset the viewport meta tag to `width=device-width, initial-scale=1, maximum-scale=1`. This forces the browser to recalculate the layout viewport at device-width, making all standard CSS techniques work correctly.
3. **`screen.width` is the only reliable mobile detection**: Unlike `window.innerWidth` (layout viewport), `screen.width` reports the physical screen dimensions in CSS pixels. It is unaffected by page content, viewport expansion, or transforms.
4. **`maximum-scale=1` is the key difference**: The test site had it, the production site didn't. This single meta tag attribute determines whether the browser can expand the layout viewport beyond device-width.
5. **Never use `setAttribute('style', ...)` with `display` on toggled elements**: It overrides the CSS show/hide mechanism. Use individual `style.xxx` properties instead, and avoid setting `display` as an inline style.
6. **Webflow sites are particularly challenging for embedded widgets**: Wide embeds, interactions engine, scroll animations, and complex layouts can all expand the layout viewport in ways that simple test pages cannot reproduce.
7. **Test on real host pages**: The widget worked perfectly on the standalone test page but broke on the Webflow production site. Always test embedded widgets in the actual hosting environment.
8. **`100vh` lies on mobile, and `visualViewport.height` is too reactive**: `100vh` includes the address bar height (clipping the bottom). `visualViewport.height` shrinks when the keyboard opens (making the popup tiny). `window.innerHeight` after a viewport meta reset is the sweet spot — correct height, stable when the keyboard opens.
9. **Don't resize the popup when the keyboard opens**: Let the browser's default behavior scroll the focused input into view above the keyboard. Resizing the popup leaves barely any visible chat area.
10. **User observation is gold**: The user's report that "pinch and zoom out makes it fill the screen" immediately pointed to the viewport/zoom issue, which led to the viewport meta reset approach.

---

## Chat Widget Auto-Scroll Removed (Twitchy Scroll UX)

### Symptom

During streaming responses, the chat widget auto-scrolled to the bottom as new content arrived. Users found this behavior twitchy — they had to pull/scroll up multiple times to stop it from snapping back to the bottom. The "smart scroll" logic (pause auto-scroll when user scrolls up, resume when at bottom) was unreliable and made the chat feel unresponsive.

### Fix (February 2026)

Removed continuous auto-scroll during streaming. Added partial auto-scroll: the chat force-scrolls to show the typing indicator and the start of the bot response, then stops — the user scrolls down at their own pace.

**What was removed:**
- `userScrolledUp` state tracking
- `handleUserScroll()` scroll detection function
- `showScrollButton()` conditional button display
- Continuous auto-scroll during streaming (previously scrolled on every chunk)
- `resetScrollState()` no longer resets scroll tracking (just hides the button)

**What was kept / added:**
- **Scroll-to-bottom arrow button** — appears when there is content below the visible area. Tapping it scrolls to the bottom and hides the button.
- **Force-scroll on user's own message** — when the user sends a message, the chat scrolls to the bottom so they can see their message appear (`scrollToBottom(true)`)
- **Force-scroll on typing indicator** — when the typing dots appear, force-scroll so the user sees VoteBot is processing
- **Force-scroll on first streaming chunk** — when the bot message element is first created, force-scroll once to show the start of the response
- **No scroll on subsequent chunks** — after the first chunk, streaming content does not auto-scroll; the arrow button appears if content extends below the visible area
- **Button auto-hides** — when the user manually scrolls to the bottom, the arrow button disappears

### Behavior Summary

| Event | Scroll behavior |
|-------|----------------|
| User sends a message | Force-scroll to bottom |
| Typing indicator appears | Force-scroll to bottom (dots visible) |
| First streaming chunk arrives | Force-scroll to bottom (response start visible) |
| Subsequent streaming chunks | No scroll — arrow button appears if content is below |
| User taps arrow button | Scroll to bottom, hide button |
| User manually scrolls to bottom | Arrow button hides |
| Session restored with history | No scroll — arrow button appears if needed |

### Files Changed

| File | Change |
|------|--------|
| `chat-widget/src/ui.js` | `showTypingIndicator()`: `scrollToBottom()` → `scrollToBottom(true)`. `appendToStreamingMessage()`: force-scroll on first chunk (when message element is created), non-forced on subsequent chunks |
| `chat-widget/dist/ddp-chat.min.js` | Rebuilt |

---

## Production Query Monitoring

### Overview

VoteBot logs all production queries and LLM responses to date-partitioned JSONL files (`logs/queries/YYYY-MM-DD.jsonl`). This data powers offline quality evaluation and performance monitoring.

### Architecture

- **`src/votebot/services/query_logger.py`**: `QueryLogger` singleton that appends JSON lines via `aiofiles`. Each entry includes `client_ip` (from `X-Forwarded-For` or direct connection) and `user_agent` for unique user analysis
- **`src/votebot/core/agent.py`**: `_log_query()` fires-and-forgets via `asyncio.create_task()` after every `process_message()` and `process_message_stream()` call
- **`scripts/evaluate_production.py`**: Offline CLI tool that reads JSONL logs, classifies queries, and validates against Webflow CMS ground truth

### Log Files Not Being Written

**Symptoms:** No files appear in `logs/queries/`.

**Check:**
1. Verify `QUERY_LOG_ENABLED` is not set to `false` in `.env`
2. Verify the `QUERY_LOG_DIR` path is writable by the uvicorn process
3. Check application logs for `"Failed to write query log entry"` warnings
4. Ensure `aiofiles` is installed: `pip install aiofiles`

### Evaluating Production Quality

```bash
# Evaluate today's queries
PYTHONPATH=src python scripts/evaluate_production.py

# Evaluate a specific date range
PYTHONPATH=src python scripts/evaluate_production.py --date 2026-02-08 --days 7

# Filter by jurisdiction with verbose output
PYTHONPATH=src python scripts/evaluate_production.py --jurisdiction FL --verbose
```

The evaluation:
1. Loads entries from JSONL files for the specified date range
2. Filters out `human_active=true` entries (Slack handoff, not LLM responses)
3. Classifies each query by entity type (bill, org, legislator, general, out-of-scope) using page_context + message regex
4. Matches against Webflow CMS ground truth using the existing `GroundTruthFetcher`
5. Validates responses using `validate_response()` from `rag_test_common.py`
6. Generates a report with pass rates, confidence analysis, citation metrics, and latency stats

### Performance Monitoring

Each log entry includes `duration_ms` (end-to-end response time). The evaluation report surfaces:
- **Average latency** across all queries
- **P95 latency** for identifying tail latency issues
- **Per-entity-type latency** for spotting slow retrieval paths

To check if the server is handling load well, look for:
- P95 latency consistently above 5 seconds (target: < 5s)
- Increasing average latency over time (indicates resource pressure)
- Low confidence scores clustered at specific times (indicates retrieval issues)

### Disk Space

JSONL files grow continuously. Each entry is ~1-3 KB. At 1,000 queries/day, expect ~3 MB/day. Consider rotating or archiving old files periodically:

```bash
# Archive files older than 30 days
find logs/queries/ -name "*.jsonl" -mtime +30 -exec gzip {} \;
```

---

## Batch Sync Worker Killed Mid-Flight

### Symptom

A batch bill sync (`POST /votebot/sync/unified` with `mode: batch`) returns `"status": "accepted"` and starts processing, but the task status never progresses past `"running"` with all-zero counts. Logs show the worker processing bills then abruptly stopping, followed by:

```
INFO:     Waiting for child process [PID]
INFO:     Child process [PID] died
```

### Root Cause

The uvicorn parent process killed the worker that was running the batch sync due to memory exhaustion. Two compounding issues:

1. **All-at-once accumulation**: `BillHandler.sync_batch()` accumulated ALL bill documents (CMS + PDF text) in a `bills` list before ingesting any of them. For 100+ bills with PDF text, this held hundreds of MB of extracted text in memory simultaneously.

2. **No PDF memory management**: PDF downloads buffered the entire response body in memory (`response.content`), pdfplumber layout caches were never flushed between pages, and Pinecone vectors were built as one giant list before batching.

The task state gets stuck as `"running"` in Redis because the completion callback (which writes final results to Redis) never fires.

### Evidence

- **2026-02-26**: Worker 57373 processing full batch sync (all jurisdictions, no limit). Last bill: FL SB 995 at 05:22:06. Worker killed at 05:22:22.
- **2026-02-27**: Worker 61235 processing batch sync. Last bill: Consolidated Appropriations Act 2026 (HR 7148) — a massive federal omnibus bill. Worker killed at 05:55:22 after 2 minutes of parsing.
- **2026-02-28**: Worker 61904 processing full batch sync (993 bills, no limit). HR 7148 generated 816 chunks from 1,540 pages (2.3MB PDF). Worker died 6 seconds after the bill following HR 7148 — OOM kill signature (no exception, no shutdown log). 130/993 bills completed (13.1%).

### How It's Fixed

**Phase 1 (Feb 2026)** — four layers of memory protection:

| Layer | File | Change |
|-------|------|--------|
| **Per-bill ingestion** | `sync/handlers/bill.py` | Process and ingest each bill immediately instead of accumulating all docs in a list |
| **Streaming PDF download** | `ingestion/sources/pdf.py` | Stream HTTP response to temp file via `aiter_bytes()` — no size limit, no in-memory buffering |
| **Page cache flush** | `ingestion/sources/pdf.py` | `page.flush_cache()` after each pdfplumber page releases layout objects |
| **Incremental vector upsert** | `services/vector_store.py` | Build and upsert Pinecone vectors one batch at a time instead of assembling the full list |

**Phase 2 (Feb 28, 2026)** — after the worker 61904 OOM kill on HR 7148 (1,540-page, 816-chunk bill), deeper memory fixes were added:

| Layer | File | Change |
|-------|------|--------|
| **Incremental embedding+upsert** | `services/vector_store.py` | Embed AND upsert in batches of 100 (previously all embeddings generated upfront before any upsert). Caps peak embedding memory at ~1.2MB instead of ~10MB for 800+ chunk bills |
| **gc.collect every bill** | `sync/handlers/bill.py` | Changed from `gc.collect()` every 10 bills to every bill. Ensures memory from a 816-chunk monster is reclaimed before the next bill starts |
| **Pipeline large-doc cleanup** | `ingestion/pipeline.py` | Explicit `del documents; del chunks; gc.collect()` after upsert when chunk count > 100 |
| **PDF page limit (safety net)** | `ingestion/sources/pdf.py` + `config.py` | Configurable `PDF_MAX_PAGES` setting (default 1000). Truncates with warning for extreme PDFs. Primary defense is better gc — this is a safety net only |
| **Zombie sync watchdog** | `main.py` | Leader worker polls Redis every 30 min for stale sync tasks (no heartbeat update in 5+ min), auto-resumes with checkpoint skip, max 3 retries before permanent failure |
| **Heartbeat tracking** | `api/routes/sync_unified.py` | Task state includes `last_heartbeat` (updated on every progress callback) and `retry_count` for zombie detection |

The daily scheduler path (`BillVersionSyncService`) also has `gc.collect()` between bills. The `pdf_max_pages` setting flows through to `_process_bill_pdf()` in both batch sync and daily version check paths.

### Investigation Steps

```bash
# Check if the worker is still alive
ps -p <PID> 2>/dev/null && echo "alive" || echo "dead"

# Find when the worker died
sudo journalctl -u votebot --since "TIME" --no-pager | grep -E "Waiting for child|Child process.*died"

# Check for OOM killer
sudo dmesg | grep -iE "oom|kill|<PID>"

# Check the orphaned task in Redis
redis-cli GET "votebot:sync:task:<task_id>"

# Monitor memory during a batch sync
watch -n 5 'ps -p $(pgrep -f "uvicorn.*votebot" | head -1) -o rss= | awk "{print \$1/1024 \" MB\"}"'
```

### Remaining Workarounds (if OOM still occurs)

1. **Use jurisdiction-scoped batches** instead of full unlimited syncs:
   ```json
   {"content_type": "bill", "mode": "batch", "jurisdiction": "MA"}
   ```

2. **Use the `limit` parameter** to cap the number of bills per batch:
   ```json
   {"content_type": "bill", "mode": "batch", "limit": 50}
   ```

### Sync Progress Reporting & Checkpoint/Resume (Feb 2026 Fix)

Two additional improvements address the "stuck at zero" and "full re-run after crash" problems:

**Live progress reporting**: The background sync runner now initializes a live result dict immediately and creates a `progress_callback` closure. Bill handlers call this after every item; the in-memory dict updates instantly (same-worker status reads see real-time counts), and Redis is flushed every 10 items (cross-worker visibility). The status endpoint returns `items_processed`, `items_successful`, `items_failed`, `chunks_created`, and `duration_ms` while status is `"running"`.

**Checkpoint/resume**: Each processed bill's Webflow ID is recorded in a Redis SET (`votebot:sync:checkpoint:{task_id}`, 24h TTL). When a worker dies mid-sync, a new request with `resume_task_id` copies the checkpoint set from the old task, and the bill handler skips items already in it:

```bash
# Resume a crashed sync
curl -X POST -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"content_type": "bill", "mode": "batch", "resume_task_id": "abc-123"}' \
  https://api.digitaldemocracyproject.org/votebot/sync/unified

# Verify checkpoints
redis-cli SCARD "votebot:sync:checkpoint:<task_id>"
```

**Files changed**: `sync/types.py` (3 new fields on `SyncOptions`), `services/redis_store.py` (3 checkpoint methods), `api/routes/sync_unified.py` (progress callback wiring + resume), `sync/handlers/bill.py` (per-bill checkpoint + skip), `sync/handlers/legislator.py` + `organization.py` (final progress callback).

### Auto-Resume via Zombie Watchdog (Feb 28, 2026 Fix)

The zombie sync watchdog runs on the leader worker and automatically detects and resumes crashed sync tasks. This eliminates the need for manual `resume_task_id` intervention after OOM kills.

**How it works:**

1. Every 30 minutes, the leader worker scans Redis for sync tasks with `status == "running"`
2. If a task's `last_heartbeat` is >5 minutes old, it's a zombie (the worker died)
3. The watchdog marks the old task as `"failed"`, copies checkpoints to a new task, and launches a resume sync
4. The resumed sync skips already-checkpointed bills via the existing checkpoint/resume mechanism
5. After 3 failed attempts (crashes), the task is marked `"permanently_failed"` with a log message and is not retried

**Key parameters:**

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Poll interval | 30 minutes | Balances detection speed vs Redis load |
| Stale threshold | 5 minutes | A 1000-page PDF takes ~2.3 min to process; 5 min leaves 2.7 min buffer |
| Max retries | 3 | Prevents infinite crash loops on consistently problematic bills |

**Verification:**

```bash
# Check if watchdog is running (leader worker logs)
sudo journalctl -u votebot --since "1 hour ago" --no-pager | grep -i "zombie\|watchdog\|auto-resum"

# Check for permanently failed tasks
redis-cli KEYS "votebot:sync:task:*" | xargs -I{} redis-cli GET {} | python -c "
import sys, json
for line in sys.stdin:
    t = json.loads(line)
    if t.get('status') == 'permanently_failed':
        print(f\"Task {t.get('error', 'unknown')}\")
"

# Monitor retry count on a specific task
redis-cli GET "votebot:sync:task:<task_id>" | python -m json.tool | grep -E "retry_count|status|last_heartbeat"
```

**Files changed:** `main.py` (`_zombie_sync_watchdog`, `_check_and_resume_stale_syncs`), `api/routes/sync_unified.py` (`last_heartbeat`, `retry_count`, `options` in task state).

### Production Fixes After First Full Batch Sync (Feb 28, 2026)

The first successful 1002-bill batch sync (all bills processed, 0 OOM) revealed three additional issues:

#### 1. False Positive Zombie Watchdog

**Symptom**: A duplicate sync was launched mid-batch even though the original was still running.

**Root cause**: During the transition from bill text ingestion → OpenStates history sync → bill version sync, the heartbeat stopped updating for ~9 minutes. The watchdog's 5-minute stale threshold triggered a false positive.

**Fix**: Added `heartbeat_callback` parameter to `BillSyncService.sync_current_session_bills()` and `BillVersionSyncService.sync_bill_versions()`. The callback is invoked every 10 bills inside their inner loops, keeping the heartbeat alive during long-running phases. A `_heartbeat()` closure in `bill.py` bridges the handler's `progress_callback` to these inner methods.

**Files changed**: `updates/bill_sync.py`, `updates/bill_version_sync.py`, `sync/handlers/bill.py`

#### 2. Webflow Date Double-Suffix Bug

**Symptom**: 2 Webflow 400 errors during bill status PATCH — dates like `2026-02-25T17:37:53+00:00T00:00:00.000Z`.

**Root cause**: `_extract_latest_action()` unconditionally appended `T00:00:00.000Z` to the date, but OpenStates sometimes returns full ISO timestamps (with `T`), not just `YYYY-MM-DD`.

**Fix**: Check for `"T"` in the date string. If already present, parse with `datetime.fromisoformat()` and normalize to Webflow format (`YYYY-MM-DDTHH:MM:SS.000Z`).

**Files changed**: `updates/bill_version_sync.py` (`_extract_latest_action()`)

#### 3. Webflow CMS Field Comparison (Rate Limit Optimization)

**Symptom**: Every checked bill made a Webflow PATCH call even when the CMS already had the correct `status` and `status-date` values.

**Fix**: Before making a PATCH call, compare existing Webflow CMS field values against the new values from OpenStates. Skip the PATCH entirely when all fields already match. This applies to both paths:

- **Unchanged-version path**: Compare `fields["status"]` and `fields["status-date"]` against OpenStates `latest_action` and `action_date`. Uses `_dates_match()` helper that normalizes dates to `YYYY-MM-DD` for comparison (handles Webflow format variations like `2026-02-25T00:00:00.000Z` vs `2026-02-25T00:00:00Z`).
- **Updated-version path**: Only include fields that actually differ from CMS in the PATCH payload (`gov-url`, `status`, `status-date`). If all three match, skip the PATCH entirely.

The batch result now tracks `webflow_skipped` count alongside `webflow_updates` and `status_updates`, visible in the final log line.

**Verification**:

```bash
# Check how many PATCH calls were skipped in a batch run
sudo journalctl -u votebot --since "1 hour ago" --no-pager | grep "webflow_skipped"

# Should see: "Bill version sync batch complete" with webflow_skipped=N
```

**Files changed**: `updates/bill_version_sync.py` (`_dates_match()`, `check_and_update_bill()`, `VersionCheckResult.webflow_patch_skipped`, `VersionSyncBatchResult.webflow_skipped`)

### Potential Further Improvements

- **Run sync in a separate process**: Decouple sync from the API workers entirely (e.g., Celery task queue, or a dedicated sync worker process)

---

## Scheduler Stops After Leader Worker Death

### Symptom

The scheduled daily bill version check (04:00 UTC) stops firing. No scheduler-related log entries appear. `redis-cli GET votebot:scheduler:leader` returns `(nil)` — no worker holds the leader lock.

### Root Cause (Historical — Fixed)

Prior to the re-election fix, only the worker that acquired the leader lock at startup would run the scheduler. If that worker was killed (e.g., by uvicorn's process manager during a heavy sync, or OOM), the lock would expire after 5 minutes but the surviving follower worker never re-checked — it had given up at startup. This left the system with **no scheduler** until a manual `systemctl restart votebot`.

### How It's Fixed

Follower workers now run a **re-election loop** (`_try_become_leader` in `main.py`). Every 60 seconds, the follower attempts `acquire_scheduler_lock()`. If the leader died and the lock expired (5-min TTL), the follower acquires it, starts the scheduler, and switches to leader mode with lock refresh.

Timeline of recovery after leader death:
1. **T+0**: Leader worker dies
2. **T+5min**: Redis lock expires (`SET NX EX 300`)
3. **T+5min to T+6min**: Follower's next `_try_become_leader` iteration acquires the lock
4. Follower logs: `"Promoted to scheduler leader via re-election"`
5. Scheduler starts, lock refresh begins (every 2 min)

### Diagnosis

```bash
# Check if any worker holds the leader lock
redis-cli GET votebot:scheduler:leader

# Check scheduler logs
sudo journalctl -u votebot --since "1 hour ago" | grep -i scheduler

# Look for re-election events
sudo journalctl -u votebot --since "1 hour ago" | grep "re-election"

# Check if scheduled jobs are registered (APScheduler)
sudo journalctl -u votebot --since "1 hour ago" | grep "Added job"
```

### Verification After Deploy

```bash
# 1. Restart the service
sudo systemctl restart votebot

# 2. Confirm one worker acquired the lock
sleep 5 && redis-cli GET votebot:scheduler:leader

# 3. Simulate leader death: find the leader PID and kill it
# (The leader PID is the worker_id prefix in the lock value)
sudo journalctl -u votebot -n 20 | grep "Scheduler started"
# kill <leader_pid>

# 4. Wait ~65 seconds, then check for re-election
sleep 70 && sudo journalctl -u votebot --since "2 minutes ago" | grep "re-election"

# 5. Confirm new lock holder
redis-cli GET votebot:scheduler:leader
```

---

## Bills With Empty Status Field in Webflow CMS

### Symptom

Some bills in Webflow CMS have an empty `status` and/or `status-date` field, even though the daily bill version check scheduler is running and updating other bills.

### Root Causes

There are five reasons a bill can be silently skipped during the daily version check, and prior to the March 2026 logging fix most of these were logged at `debug` level (invisible in production):

1. **No OpenStates URL** — The bill's `open-states-url-2` field in Webflow CMS is empty. The version check silently skips it.

2. **Not current session** — `is_current_session()` returns False. This had multiple sub-bugs fixed in March 2026 (see [Nightly Bill Sync Skips Bills](#nightly-bill-sync-skips-bills-for-certain-states) below).

3. **Jurisdiction not scheduled today** — `should_sync_jurisdiction()` returns False for states not in session on non-Mondays. These bills are only checked once a week.

4. **No `latest_action_description` from OpenStates** — If OpenStates returns null/empty for the latest action, `_extract_latest_action()` returns `(None, ...)` and the status update is skipped entirely. The bill is counted as "unchanged" with no error.

5. **Webflow PATCH fails silently** — `update_bill_fields()` returns `False` on non-200 responses (e.g., date validation errors). The caller counts the bill as "unchanged" or "updated" regardless — it is never counted as "failed" in the batch summary. Historical cause: the date double-suffix bug (fixed Feb 28 in `a0e06e8`) produced invalid dates like `2026-01-20T22:38:26+00:00T00:00:00.000Z`.

Additionally, bills synced before the status feature was added (pre Feb 25, 2026) never received an initial status and depend entirely on the daily version check to backfill.

### Fix (March 2026) — Enhanced Logging

All silent skip/failure paths now log at `info` or `warning` level with bill identifiers (`webflow_id`, `slug`, etc.):

| Log message | Level | Meaning |
|---|---|---|
| `Skipping bill (no OpenStates URL)` | info | Missing `open-states-url-2` in CMS |
| `Skipping bill (not current session)` | info | Session year doesn't match current year |
| `Skipping bill (jurisdiction not scheduled today)` | info | Off-session state, not Monday |
| `Bill has no versions in OpenStates` | warning | OpenStates returned empty versions array |
| `No latest_action from OpenStates — status not updated` | warning | OpenStates has no action data |
| `Webflow status PATCH failed (version unchanged path)` | warning | PATCH returned non-200 |
| `Webflow PATCH failed (new version path)` | warning | PATCH returned non-200 (includes field_data) |

New counters on `VersionSyncBatchResult` (visible in both "Bill version sync batch complete" and "Daily bill version check completed"):

- `skipped_no_url` — bills missing OpenStates URL
- `skipped_not_current` — bills from old sessions
- `skipped_jurisdiction` — bills in off-session jurisdictions
- `webflow_patch_failures` — bills where PATCH failed or no action data (not updated AND not skipped)
- `no_latest_action` — bills with no action from OpenStates

The scheduler completion log now warns (`logger.warning`) when `webflow_patch_failures > 0`, not just on hard failures.

### Diagnostic Steps

```bash
# 1. Check batch completion summary (look at new counters)
sudo journalctl -u votebot --since "1 day ago" --no-pager | grep "bill version sync batch complete\|Daily bill version check completed"

# 2. Find specific bills with missing status
sudo journalctl -u votebot --since "1 day ago" --no-pager | grep "no OpenStates URL\|No latest_action\|PATCH failed"

# 3. Find skipped bills by reason
sudo journalctl -u votebot --since "1 day ago" --no-pager | grep "Skipping bill"

# 4. Find bills with no versions in OpenStates
sudo journalctl -u votebot --since "1 day ago" --no-pager | grep "no versions in OpenStates"
```

### Files Changed

- `updates/bill_version_sync.py` — All skip/failure logging promoted from debug to info/warning; new batch counters
- `updates/scheduler.py` — Completion log includes new counters; warns on PATCH failures

---

## Nightly Bill Sync Skips Bills for Certain States

### Symptom

The daily bill version check skips all bills for certain states (WA, MI, UT, AZ, MA) with log message "Skipping bill (not current session)". Bills in those states never receive status or status-date updates.

### Root Causes (Three Bugs) — All FIXED March 2026

**Bug 1: OpenStates returns `end_date=None` for in-progress sessions (WA, UT)**

`_check_live_sessions()` in `legislative_calendar.py` required both `start_date` and `end_date` to be non-None. Sessions that are still in progress have `end_date=None` in OpenStates, causing them to be treated as inactive.

**Fix:** Treat sessions with `start_date <= today` and no `end_date` as active.

**Bug 2: OpenStates returns stale `end_date` for multi-year sessions (MI)**

Michigan's `2025-2026` session has `end_date=2025-12-31` in OpenStates (the end of the first year). In 2026, the date check fails even though the session identifier spans both years.

**Fix:** When `end_date` has passed, parse years from the session identifier (e.g., `"2025-2026"` → `[2025, 2026]`). If the current year falls within that range, treat the session as active.

**Bug 3: Non-standard session identifiers + wrong Webflow field name (AZ, MA)**

Arizona uses `57th-2nd-regular` as its session identifier (no 4-digit year). The sync regex `is_current_session()` couldn't parse it. Additionally, the code read `fields.get("session-year", "")` but this Webflow field doesn't exist — the actual field is `bill-session` (returns integer, e.g., `2026`). This meant `session_year` was always empty string for **all states**, not just AZ.

**Fix (two parts):**
1. Switched from `is_current_session()` (sync, regex-only) to `is_current_session_async()` which queries the OpenStates API for the current session identifier and matches directly.
2. Fixed the Webflow field name from `session-year` to `bill-session` with `str()` cast (Webflow returns integer).

### Affected Files

| File | Fix |
|------|-----|
| `votebot/utils/legislative_calendar.py` | `_check_live_sessions()` — null end_date + stale end_date |
| `ddp-sync/services/legislative_calendar.py` | Same fix (ddp-sync has its own copy) |
| `ddp-sync/pipelines/bill_version.py` | `is_current_session_async()` + `bill-session` field |
| `ddp-sync/pipelines/bill_sync.py` | Same field fix + async session check |

### Commits

- `cf41609` / `eccf386` — Null end_date fix (votebot / ddp-sync)
- `cbf49a8` / `d5e9eda` — Stale end_date for multi-year sessions (votebot / ddp-sync)
- `13389cf` — Async session check for AZ (ddp-sync)
- `f90795c` — Webflow field `session-year` → `bill-session` (ddp-sync)

### Webflow CMS Bill Fields Reference

| Webflow Field | Type | Example | Used For |
|---------------|------|---------|----------|
| `session-code` | string | `"2026"`, `"119"`, `"57th-2nd-regular"` | OpenStates session matching |
| `bill-session` | integer | `2026` | Calendar year (heuristic fallback) |
| `open-states-url-2` | string | `https://openstates.org/az/bills/57th-2nd-regular/SB1068/` | Jurisdiction + session extraction |

> **Note:** There is no `session-year` field in Webflow CMS. Any code referencing `session-year` is reading a nonexistent field and getting empty string.

---

## DDP-Sync Issues

> **Note:** Sync and data pipelines have moved from VoteBot to ddp-sync (a separate service on port 8001). Check ddp-sync logs for sync-related issues: `sudo journalctl -u ddp-sync -n 100 --no-pager`

### Redis Health Check Error: `'RedisStore' object has no attribute '_redis'` — FIXED

**Discovered:** 2026-03-11 during Phase 7 deployment
**Fixed:** 2026-03-11 (commit `8a15de9`)

**Root cause:** The health check and zombie watchdog in ddp-sync accessed `store._redis` but the `RedisStore` attribute is `_client`. Also fixed: `VectorStoreService` eagerly created a Pinecone client in `__init__`, crashing on startup without `PINECONE_API_KEY`. Changed to lazy initialization.

### Pinecone Crashes on Startup Without API Key — FIXED

**Discovered:** 2026-03-11 during local development testing
**Fixed:** 2026-03-11 (same commit as above)

**Root cause:** `VectorStoreService.__init__` called `Pinecone(api_key="")` which raises `PineconeConfigurationError`. Fixed by making the Pinecone client a lazy `@property` — only created on first use, not at import time.

### Scheduler Shows 0 Jobs (Config Not Found) — FIXED

**Discovered:** 2026-03-11 during local development testing
**Fixed:** 2026-03-11 (commit `8a15de9`)

**Root cause:** Non-editable pip install (`pip install .`) resolves `Path(__file__).parent.parent.parent` to `site-packages/`, not the repo root. Fixed by adding a CWD fallback: checks both the package-relative path and `Path.cwd() / "config" / "sync_schedule.yaml"`.

### Python 3.13 Editable Install Fails on macOS

**Symptom:** `pip install -e .` succeeds but `import ddp_sync` fails. Venv created with Homebrew Python 3.13 doesn't process `.pth` files correctly for editable installs.

**Workaround:** Use non-editable install: `pip install .` (requires reinstall after each code change).

---

## Getting Help

If these troubleshooting steps don't resolve your issue:

1. Check the application logs for errors
2. Review recent changes to sync code or data sources
3. Test with a minimal reproducible example
4. File an issue at https://github.com/Digital-Democracy-Project/votebot/issues
