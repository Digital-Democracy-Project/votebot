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

### Comparison: Bill vs Legislator Filter Chains

| Aspect | Bills | Legislators |
|--------|-------|-------------|
| Primary filter | `webflow_id` | `legislator_id` (OpenStates ID) |
| Fallback 1 | `slug` | `webflow_id` |
| Fallback 2 | — | `slug` |
| Pre-retrieval resolution | `_lookup_bill_slug()` (from query) | `_resolve_legislator_id()` (from Webflow CMS) |

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
| `session-year` | (not used) | (not used) | Calendar year only - don't use for OpenStates |
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

**Fix (implemented February 2026)**: Added `_is_organization_query()` detection and `_retrieve_organization_priority()` to `retrieval.py`. This:
1. Detects org-focused queries by checking for strong indicators ("organization", "nonprofit", "501(c)")
2. Routes to a dedicated retrieval path that searches `document_type="organization"` first
3. Fetches ALL chunks for the top matching org by `document_id` to capture bill positions in separate chunks

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

### Root Cause (Two Bugs)

#### Bug 1: LLM drowns in voter data for large bills

The `_prefetch_bill_info` method fetches the complete bill info from OpenStates, which for HR 1 includes 435+ voter names grouped by party. The formatted document lists up to 20 names per party per vote position. For a bill where 215 Republicans voted YES and only 2 voted NO, the YES list dominates the context.

The LLM sees Buchanan is a Republican, sees the bill passed along party lines, and confidently concludes he voted YES — without carefully checking the NO list where Buchanan actually appears.

**Fix**: When on a legislator page and a bill is mentioned, `_prefetch_bill_info` now also calls `lookup_legislator_vote()` for the specific legislator (using `page_context.title`). The result is prepended to the context as a `## SPECIFIC VOTE LOOKUP RESULT` section with a note that it should be used as the definitive answer. This gives the LLM an unambiguous, authoritative answer instead of requiring it to find one name in hundreds.

```python
# NEW — in _prefetch_bill_info, after fetching bill info:
if page_context and page_context.type == "legislator" and page_context.title:
    vote_result = await self.bill_votes.lookup_legislator_vote(
        legislator_name=page_context.title,
        jurisdiction=jurisdiction,
        session=session,
        bill_identifier=bill_identifier,
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

### Fix (February 2026)

Both bugs fixed in `src/votebot/core/agent.py`:

| Bug | Location | Fix |
|-----|----------|-----|
| LLM ignores minority vote | `_prefetch_bill_info` | Also call `lookup_legislator_vote()` on legislator pages and prepend specific vote to context |
| Legislator ID used as bill ID | `_verify_legislator_vote` line ~1120 | Only use `page_context.id` as `bill_identifier` when `page_context.type == "bill"` |

### How It Works After Fix

```
User on legislator page (Vern Buchanan): "i'm talking about HR 1"
                                          ↓
_prefetch_bill_info():
  bill_identifier = "HR1"
  jurisdiction = "US", session = "119"
  → get_bill_info() returns full bill data
  → ALSO: lookup_legislator_vote("Vern Buchanan", "US", "119", "HR1")
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

### Why This Only Affects Legislator Pages

On a **bill page**, both bugs are irrelevant:
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

## Getting Help

If these troubleshooting steps don't resolve your issue:

1. Check the application logs for errors
2. Review recent changes to sync code or data sources
3. Test with a minimal reproducible example
4. File an issue at https://github.com/VotingRightsBrigade/votebot/issues
