# PLAN: Quick-Action Buttons with Response Caching

**Date:** 2026-03-30 (rev 2026-04-28a/b/c: Fix D scope correction + Fix F + PM review iterations; rev 2026-04-29a: Fix F shipped + verified; rev 2026-04-29b: Phases 2/3/5 implemented + PM-review polish)
**Status:** v8 — All phases (1–5) implemented; Phase 4 (flag enable) is the only remaining manual step

**Motivation:** Analysis of 1,927 production queries shows 3 dominant question patterns that account for ~63% of first messages on bill pages. Adding quick-action buttons improves UX (one-tap access to common answers) and, with caching, reduces AI token usage and latency for summary and pros/cons responses. This also directly fixes the stale bill status problem (HR 7147 incident) by ensuring status queries trigger the live OpenStates lookup.

**Data basis:** 323 first-message-in-conversation sessions on bill pages, 1,233 total bill queries across all logs (Feb 10–Mar 30, 2026).

---

## Proposed Buttons

Three buttons shown below the chat input on bill pages:

| Button Label | % of First Messages | Avg Latency | Avg Response | Cached? |
|---|---|---|---|---|
| **"Summarize this bill"** | 43.7% | 12.8s | ~650 tokens | Yes (until next bill sync) |
| **"Pros and cons"** | 8.4% | 8.1s | ~390 tokens | Yes (until next bill sync) |
| **"Latest status & votes"** | 11.1% (status + votes) | 9.2s | ~326 tokens | **Never cached** — always live OpenStates |

### Why These Three

1. **Summarize this bill** — The #1 first question (43.7%), and it drives the massive follow-up editing chain that accounts for 34% of all queries. A cached summary means instant first response, then users can iterate via the normal LLM pipeline.

2. **Pros and cons** — The #3 first question (8.4%), and a high-engagement pattern — users frequently ask for more pros/cons after the initial response. A balanced starting point gets users into the conversation faster.

3. **Latest status & votes** — Combines status (3.4%) and vote history (7.7%) into one button. This button **never uses cache** — it always hits OpenStates live to guarantee fresh data. The button message includes the bill identifier from page context (e.g., "What is the latest status and vote history for HR 7147?"), which guarantees `_should_use_bill_votes_tool()` fires.

### Buttons Not Included

- **"Who sponsors this bill?"** (8.4%) — Common, but low-effort and usually a one-shot question.
- **"How does this bill affect X?"** — Too varied; no single prompt covers it.
- **Summary editing** ("make it concise") — Follow-ups, not starters. The button gives them a starting point.

---

## Response Caching

### What Gets Cached and What Doesn't

| Button | Cached? | Rationale |
|---|---|---|
| Summary | Yes | Bill text changes only on sync. Cache until next sync. |
| Pros and cons | Yes | Arguments tied to bill text. Cache until next sync. |
| Status & votes | **No** | Always hits OpenStates live. This is the whole point of fixing stale data. |

### Architecture

**Two separate responsibilities across two services:**

- **ddp-sync** — Detects bill amendments and publishes cache invalidation events. Does NOT generate or populate cached responses. ddp-sync has no LLM access.
- **VoteBot** — Listens for invalidation events, manages the Redis cache, generates cached responses lazily on first button tap after invalidation.

```
=== Cache Invalidation (ddp-sync) ===

ddp-sync detects bill amendment
    ↓
Re-ingests updated bill text to Pinecone
    ↓
Publishes {"slug": "...", "reason": "bill_version_change"} on Redis channel votebot:cache:invalidate
    ↓
VoteBot receives event → deletes cached button responses for that slug
    ↓
Done. Cache is now empty for that bill. Next button tap will regenerate.

=== Cache Population (VoteBot — lazy, on button tap) ===

User taps "Summarize" or "Pros and cons" button
    ↓
Widget sends: { message: "...", button: "summary", page_context: { slug: "...", ... } }
    ↓
VoteBot checks Redis cache:
  key = "votebot:button:{slug}:{button_type}"
    ↓
  HIT  → Return cached response (skip LLM), log as cache_hit=true
  MISS → Normal RAG + LLM pipeline → cache response in Redis → return

User taps "Latest status & votes" button
    ↓
Widget sends: { message: "What is the latest status and vote history for {bill_id}?", button: "status_votes", ... }
    ↓
VoteBot: NO cache check. Always runs full pipeline with bill_votes tool enabled.
```

**Why lazy population (not eager warming):** ddp-sync has no LLM access and shouldn't need it — it's a data pipeline, not a chat service. Eager warming would require ddp-sync to call a VoteBot API endpoint to trigger response generation, adding coupling and complexity. At current traffic levels, the first user after a cache miss waits the normal 8–13s, then all subsequent users get instant responses. Worth revisiting if a bill goes viral and hundreds of users hit it simultaneously.

### Cache Keys

Slug is the unique identifier — it comes from Webflow and is already unique across all jurisdictions and sessions. No need for composite keys.

| Button | Redis Key | TTL |
|---|---|---|
| Summary | `votebot:button:{slug}:summary` | No TTL — persists until invalidated by sync |
| Pros and cons | `votebot:button:{slug}:pros_cons` | No TTL — persists until invalidated by sync |

### Cache Invalidation

**Primary mechanism: amendment-triggered.** Cache is invalidated only when ddp-sync detects a **new bill version** (amendment, engrossment, substitute, etc.) and successfully re-ingests the updated text into Pinecone.

**Trigger point in ddp-sync:**
- **File:** `ddp-sync/src/ddp_sync/pipelines/bill_version.py`, ~line 522
- **When:** After `_ingest_bill_text()` succeeds and `set_bill_version()` updates the Redis version cache
- **Hook:** Publish `{"slug": bill_slug}` on Redis channel `votebot:cache:invalidate`
- **Why here (not earlier):** Ensures new Pinecone chunks exist before invalidating the cached response, so the next button tap generates from fresh data

**Version detection criteria** (from `_is_newer_version()`, line 149):
1. No cached version exists (first sync)
2. Version date is newer than cached
3. Same date but different version note (e.g., "Introduced" → "Engrossed")
4. Text URL changed

Bills that are no longer in session and not being amended will keep their cached responses indefinitely — this is correct behavior since their content is stable.

**Safety net TTL:** 7-day max TTL on all cache keys. This is a safety net only — the primary invalidation mechanism is the ddp-sync amendment event. The TTL catches edge cases like a bill being removed from Webflow entirely, where ddp-sync would never publish an invalidation.

**Startup reconciliation:** Redis pub/sub is fire-and-forget — if VoteBot is down when ddp-sync publishes an invalidation, the event is lost. To handle this, on VoteBot startup, run a reconciliation check:

1. Scan all `votebot:button:*` keys in Redis
2. For each cached entry, compare its `cached_at` timestamp against the bill's `last_checked` timestamp in the `ddp:bill_version:{webflow_id}` key that ddp-sync already maintains
3. If the bill was re-ingested after the cache was generated, delete the cached entry

This runs once on startup — no new infrastructure, no polling. Combined with the 7-day TTL, this guarantees eventual consistency even through deploys and restarts.

**Manual:** Admin endpoint `DELETE /votebot/v1/cache/button/{slug}` to force-clear a specific bill's cache.

**Slug immutability:** Webflow slugs are unique and immutable — they never change after creation. No slug-change handling is needed.

### What Gets Cached

The cached response includes:
- `response` text (the LLM output)
- `citations` list (so the UI can render citation links)
- `confidence` score
- `cached_at` timestamp (ISO format)
- `button_type` (for logging)

Follow-up messages after a cached button response go through the normal LLM pipeline — only the initial button tap is cached.

### Token Savings Estimate

Based on production logs (summary + pros/cons only, status is never cached):

- **Pros/cons**: 37 cache hits × ~390 output tokens = **~14,400 tokens saved**
- **Summary**: 4 cache hits × ~653 output tokens = **~2,600 tokens saved**
- **Total: ~17,000 output tokens saved** over the log period (7 weeks)
- Plus input token savings (no retrieval + prompt construction on cache hits)
- Plus latency savings: cache hits serve in <100ms vs. 8–13s for LLM calls
- Savings scale linearly with traffic — as more users view the same bills, hit rate increases

### Redis Memory Estimate

- Average cached response: ~2KB (response text + citations JSON)
- Active bills in Pinecone: ~1,000
- Max cached entries: 1,000 bills × 2 button types = 2,000 keys
- **Total: ~4MB** — negligible relative to Redis capacity

---

## Feature Flag

Gate the entire feature behind an env var:

```python
# config.py
quick_action_buttons_enabled: bool = False  # env: VOTEBOT_QUICK_ACTION_BUTTONS
```

When disabled:
- Backend ignores `button` metadata in WebSocket payloads (processes as normal message)
- Widget does not render buttons (checks config flag from a `/votebot/v1/features` endpoint or a WebSocket handshake field)
- Cache is not read or written

This allows instant rollback without a redeploy — just set the env var and restart.

---

## Logging and Observability

Every button interaction must be logged for performance analysis:

**In `query_processed` events, add fields:**
- `button_type: str | None` — which button was clicked ("summary", "pros_cons", "status_votes", or null for free-typed)
- `cache_hit: bool | None` — whether the response was served from cache

**File:** `src/votebot/services/query_logger.py` — add `button_type` and `cache_hit` parameters to `log_event()`.

**Success metrics to track:**
- **Button CTR** — % of sessions where a button is clicked vs. free-typed first message
- **Cache hit rate** — % of summary/pros_cons button taps served from cache
- **Latency comparison** — cache hit vs. miss response times
- **Follow-up rate** — % of button-initiated conversations that continue with follow-up messages (indicates engagement, not abandonment)
- **Token savings** — actual output tokens saved (cache hits × avg cached response tokens)

These metrics are computed from the existing JSONL logs during manual analysis — no new dashboards needed.

---

## Accessibility

Buttons must be accessible per WCAG 2.1 AA (required for a civic nonprofit):

- Keyboard navigable (Tab to focus, Enter/Space to activate)
- `role="button"` and `aria-label` attributes for screen readers
- Visible focus indicators matching the widget's primary color
- Buttons must not be the only way to access these features — users can always type the same questions

**Example markup:**
```html
<div class="ddp-quick-actions" role="group" aria-label="Quick questions about this bill">
    <button role="button" aria-label="Summarize this bill" data-action="summary">Summarize this bill</button>
    <button role="button" aria-label="Pros and cons" data-action="pros_cons">Pros and cons</button>
    <button role="button" aria-label="Latest status and votes" data-action="status_votes">Latest status &amp; votes</button>
</div>
```

---

## Fix for Stale Status Data (HR 7147 Problem)

Two independent fixes:

### Fix A: Button ensures bill identifier in message

The "Latest status & votes" button constructs a message with the bill identifier:
```javascript
"What is the latest status and vote history for HR 7147?"
```
This guarantees `_should_use_bill_votes_tool()` fires (line 1050: `has_bill_identifier` matches).

### Fix B: Bill page status queries always trigger live lookup

Modify `_should_use_bill_votes_tool()` to also fire when the user is on a bill page AND the message contains status/action keywords — even without a bill identifier in the message text:

**File:** `src/votebot/core/agent.py`, in `_should_use_bill_votes_tool()`:
```python
def _should_use_bill_votes_tool(
    self,
    rag_confidence: float,
    message: str,
    page_context=None,  # NEW parameter
) -> bool:
    # ... existing logic ...

    # NEW: On bill pages, status/action queries should always use live data
    is_bill_page_status_query = False
    if page_context and page_context.type == "bill":
        status_keywords = ["status", "action", "latest", "passed", "failed", "where is", "what happened"]
        is_bill_page_status_query = any(kw in message_lower for kw in status_keywords)

    should_enable = (
        is_vote_query or
        has_bill_identifier or
        is_bill_page_status_query or  # NEW
        (rag_confidence < very_low_threshold and is_bill_inquiry)
    )
```

This requires passing `page_context` to the method at its two call sites (~lines 381 and 565).

### Fix C: Resolve bill identifier from Webflow slug

`_prefetch_bill_info()` had a gap: when on a bill page without `page_context.id`, it couldn't resolve the bill for OpenStates lookup — even though the slug was always available. Added Method 5: look up the bill in Webflow CMS via slug to get the authoritative identifier and jurisdiction. This uses the existing `get_bill_details(slug=slug)` service. Shipped in commit `ee1227a`.

### Fix D: Remove bill-history from Pinecone retrieval (PARTIAL — completed by Fix F)

Stale `bill-history` chunks from Pinecone were being injected into the LLM context alongside live OpenStates data, causing conflicting information. Removed Phase 3 (`document_type: "bill-history"`) from the retrieval pipeline entirely. (Commit `b518132`)

**What this fix did NOT cover** — discovered during 2026-04-28 production log audit:

- The fix only patched `_retrieve_bill_with_text_priority()` (the bill-page retrieval path). The general-page retrieval path at `retrieval.py:206-211` does an unfiltered semantic query (`filter=None`) that still surfaces bill-history chunks via similarity match.
- The unfiltered fallback at `retrieval.py:572-583` (runs when all typed phases return empty) is also still capable of leaking bill-history on bill pages, though logs show it has not yet triggered in production.
- ddp-sync (`pipelines/bill_sync.py:982-1011`) **still actively produces** bill-history docs on every bill sync. Even after a full Pinecone flush, the next sync would repopulate.

Production log analysis (Mar 31 – Apr 28): 71 of 71 post-deploy bill-history retrievals occurred on `general` pages, confirming the read-path leak is exclusively in the unfiltered general-query path. Fix F closes the data layer entirely so no read path can surface bill-history regardless of filter coverage.

### Fix E: Pass full action history to LLM

Two instances of reverse-order action slicing were sending the oldest actions to the LLM instead of the newest. `_fetch_bill_info()` used `actions[-10:]` and `format_bill_info_document()` used `actions[-5:]` — both grabbing from the wrong end since OpenStates returns newest-first. Removed all action limits; the LLM now receives the complete history. (Commits `d0fed09`, `6f756c7`)

### Fix F: Complete bill-history removal at the data layer (NEW — 2026-04-28)

Closes the gap left by Fix D. Three coordinated changes across both repos.

**F1. ddp-sync — stop producing bill-history docs**

**File:** `ddp-sync/src/ddp_sync/pipelines/bill_sync.py`

- Delete `format_bill_history_chunk()` method (lines 541-683). Only caller is `sync_bill()`, which is also being modified.
- In `sync_bill()`, delete the bill-history production block: lines 981-1011 (the `history_chunk` initialization, the `DocumentMetadata` for bill-history, and the first `ingest_document()` call inside the `try` block).
- Keep `extract_metadata_from_openstates()` call (line 985) and the `extra_metadata["slug"] = bill_slug` assignment — both are still needed by the bill-votes branch (line 1026).
- Keep the bill-votes branch (lines 1013-1041) untouched. `bill-votes` docs remain the data source for the `legislator-votes` reverse index built by `build_legislator_votes.py`.

**File:** `ddp-sync/src/ddp_sync/sync/handlers/bill.py`

- Delete line 164: `document_ids.append(f"bill-history-{item_id}")`. This list is purely for accounting; harmless if left, but cleaner to remove.

**Operational baseline shift:** After this change, `sync_bill()` only produces `bill-votes` docs from OpenStates data per bill. `chunks_created` per bill will roughly halve. This is the new baseline — not a regression. Document in:
- The deploy commit message
- The DDP-Sync README (under sync metrics section)
- Any internal Slack/Notion sync-status notes Ramon checks during sync runs

If sync-status alerting is configured anywhere on `chunks_created` thresholds, adjust the threshold or temporarily mute during the deploy window.

**Deploy verification before proceeding to F2:**

ddp-sync runs as a separate service (port 8001 per README). Before running F2, verify the new code is live and old workers are drained:

1. After `git pull && systemctl restart ddp-sync` (or equivalent), confirm the running process is on the new commit (e.g., `systemctl status ddp-sync` or check process start time).
2. Trigger a single-bill on-demand sync via the unified API and confirm the response does NOT include any `bill-history-*` document IDs.
3. Only then proceed to F2. If old workers are still serving requests due to systemd lingering, repeat step 2 until consistently clean.

**F2. Pinecone flush — delete existing bill-history chunks**

**File (new):** `votebot/scripts/flush_bill_history.py`

One-shot script that calls `vector_store.delete(filter={"document_type": "bill-history"})`. The existing `VectorStore.delete()` method (`src/votebot/services/vector_store.py:255-286`) already supports filter-based deletion via Pinecone's metadata-filter delete API. Doc IDs are uniquely prefixed (`bill-history-{webflow_id}`); zero risk to other document types.

**Scale:** ~1,170 bills currently in Pinecone (per Webflow CMS count). Bill-history docs are short single-page text blocks, typically chunked into 1–3 vectors each. Estimated total deletion: ~1,500–3,500 vectors. Well within Pinecone's filter-delete capacity for a single call. Script should still wrap the call in a tenacity retry decorator (3 attempts, exponential backoff) matching the pattern used elsewhere in `vector_store.py`.

**Pre-flight check (before delete):**
```python
# Count what we're about to delete
results = await vector_store.query(
    query="legislative history",  # broad semantic query
    top_k=10000,
    filter={"document_type": "bill-history"},
)
pre_count = len(results)
print(f"About to delete {pre_count} bill-history vectors")
# Prompt for confirmation before proceeding
```

**Post-delete verification and persisted count:**
```python
# After delete, re-query to confirm zero remaining
post_results = await vector_store.query(
    query="legislative history",
    top_k=10000,
    filter={"document_type": "bill-history"},
)
post_count = len(post_results)
deleted = pre_count - post_count

# Log to stdout AND append to a persistent record for the eval pipeline
print(f"Deleted {deleted} bill-history vectors. Remaining: {post_count}")
# Write to logs/eval/flush_bill_history.json so evaluate_production.py
# can include it in subsequent reports
record = {
    "timestamp": datetime.utcnow().isoformat(),
    "pre_count": pre_count,
    "post_count": post_count,
    "deleted": deleted,
}
Path("logs/eval/flush_bill_history.json").write_text(json.dumps(record, indent=2))
```

If `post_count > 0`, that's a partial-delete signal. Re-running the script with the same filter is idempotent (Pinecone metadata-filter delete is a set operation, not a sequence of individual deletes), so a second run is safe and should drive the count to zero. If a second run still leaves vectors, escalate — likely a Pinecone API issue requiring support contact.

**Reversibility:** Bill-history docs are fully reconstructable from OpenStates. If a rollback is ever needed:
1. Revert the F1 commit in ddp-sync (`format_bill_history_chunk` and the production block).
2. Run `python scripts/sync.py bill --batch --jurisdiction <code>` (or backload all) — the restored producer regenerates the chunks from current OpenStates data.

This means F2 is recoverable in ~1 batch sync run, not a permanent data loss event. Worth keeping the deletion script and the F1 diff easily revertible (single commit each, not bundled with other changes).

**Optional defensive backup** (can skip if Ramon is comfortable with reconstruct-from-OpenStates as the rollback path):
- Before deletion, dump bill-history doc IDs and metadata to a local JSON file via `vector_store.list()` for manual inspection if needed later.
- This is belt-and-suspenders; the OpenStates regeneration path makes it strictly optional.

Run after F1 is deployed and verified per the deploy verification step above. Running before would leave a window where the next ddp-sync run repopulates the chunks.

**F3. votebot — extend live-tool trigger to read conversation history**

**File:** `votebot/src/votebot/core/agent.py`

Closes the only remaining UX gap exposed by F1+F2: a user mid-conversation about HR 7147 asking "is it still active?" without re-naming the bill. The current `_should_use_bill_votes_tool()` only inspects the current message, so this kind of follow-up doesn't trigger the live tool. With bill-history gone from Pinecone, these queries would otherwise get a vague summary-based answer instead of fresh OpenStates data.

Modify `_should_use_bill_votes_tool()` (line 992) to accept `conversation_history`:

```python
def _should_use_bill_votes_tool(
    self,
    rag_confidence: float,
    message: str,
    page_context=None,
    conversation_history: list[dict] | None = None,  # NEW
) -> bool:
    # ... existing logic ...

    # NEW: When current message lacks a bill identifier, scan USER messages
    # in conversation history. Bot messages are excluded — bot answers can list
    # tangentially-mentioned bills (e.g., "...similar bills like HB 12 and SB 34...")
    # which would otherwise trigger the live tool for a bill the user never asked about.
    has_bill_in_history = False
    if not has_bill_identifier and conversation_history:
        for msg in reversed(conversation_history[-6:]):
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "") or ""
            extracted_id, _ = self._extract_bill_from_text(content)
            if extracted_id:
                has_bill_in_history = True
                break

    should_enable = (
        is_vote_query or
        has_bill_identifier or
        has_bill_in_history or  # NEW
        is_bill_page_status_query or
        (rag_confidence < very_low_threshold and is_bill_inquiry)
    )
```

Update both call sites to pass `conversation_history`:

- Line 381 (`process_message`): `enable_bill_votes = self._should_use_bill_votes_tool(rag_confidence, message, page_context, conversation_history)`
- Line 565 (`process_message_stream`): same, both already have `conversation_history` in scope.

**Backwards compatibility:** The new `conversation_history` parameter has `= None` default, so any existing or future caller that doesn't pass it continues to behave as before (no history scan, just current-message inspection). No breaking change.

**Codebase audit step before merging F3:** Run `grep -rn "_should_use_bill_votes_tool" src/` to confirm only the two known call sites at lines 381 and 565 exist. If any test fixture or other internal caller is found, update it to pass `conversation_history` explicitly (or leave it relying on the default).

Reuses the existing `_extract_bill_from_text()` helper at line 1882 — same regex patterns used by `_prefetch_bill_info` Method 3 and `_verify_legislator_vote`. No new extraction logic.

**Mirror change in `_prefetch_bill_info` Method 3 (line 1182):**

Method 3 has the same false-positive risk — it currently scans all roles and would resolve to a bot-mentioned bill that the user never asked about. To preserve the "F3 and Method 3 agree by construction" guarantee, apply the same user-only filter to Method 3:

```python
# Method 3: Search USER messages in conversation history for bill identifiers
if not bill_identifier and conversation_history:
    for msg in reversed(conversation_history[-6:]):
        if msg.get("role") != "user":  # NEW: skip bot messages
            continue
        content = msg.get("content", "")
        extracted_id, extracted_jurisdiction = self._extract_bill_from_text(content)
        if extracted_id:
            bill_identifier = extracted_id
            ...
```

Without this mirror change, F3 might decide "fire the tool" (no bill found in user history → tool doesn't fire) while Method 3 still resolves a bot-mentioned bill (or vice versa). With both filtering to user-only, the agreement holds.

**Multi-bill conversation handling:**

The reverse iteration (`for msg in reversed(conversation_history[-6:])`) returns the **most recently mentioned** bill identifier first. Same iteration order as `_prefetch_bill_info` Method 3 (line 1182), which is the function that actually does the bill resolution after F3 decides to fire the tool.

Because F3 and `_prefetch_bill_info` use **identical extraction logic on the same input**, they are guaranteed to agree on which bill to use. F3's job is binary (fire the tool or not); `_prefetch_bill_info` does the resolution. There is no possibility of F3 firing the tool for one bill while `_prefetch_bill_info` fetches a different one.

Edge cases and behavior:
- **Single bill in history** → tool fires with that bill. ✓
- **Multiple bills in history** → tool fires with the most recent (last-mentioned by turn order). ✓
- **No bill in history, no bill in current message** → tool does NOT fire, falls through to existing low-confidence + bill_inquiry heuristic. Same as today.
- **Conversation history beyond 6 turns** → ignored. Matches the existing convention used by `_prefetch_bill_info` Method 3 — kept consistent intentionally so users don't see different behavior between the firing decision and the resolution.

**Performance:**
The scan adds up to 6 regex evaluations per message in the worst case (when current message has no bill ID). Each regex runs against a single message string (typically <1KB). Negligible vs. the existing retrieval and LLM-call overhead. No load test added — would be over-engineered for this scope.

**Why F1 + F2 + F3 together:** F1 stops the producer, F2 cleans the existing leak, F3 preserves UX for the rare conversational-followup case where the user has lost their explicit bill identifier. F1 alone would let stale chunks linger; F2 alone would be reverted by the next sync; F3 alone doesn't solve the leak.

**Tests:**

*F1:*
- After deploy, run a single-bill on-demand sync (`/votebot/v1/sync/unified` with `mode=single`) and confirm no `bill-history-*` doc IDs appear in the response.
- Query Pinecone directly: `vector_store.query(filter={"document_type": "bill-history"}, top_k=10)` should return only pre-existing chunks (will be cleaned up by F2). Critically, the count should NOT increase across multiple test syncs.

*F2:*
- After flush, query Pinecone: `vector_store.query(filter={"document_type": "bill-history"}, top_k=10)` should return zero matches.
- Run a single-bill sync after the flush and re-query — count should stay at zero (verifies F1 is holding).

*F3 — expanded test matrix:*
1. **Happy path**: turn 1 user "Tell me about HR 7147" → bot responds → turn 2 user "is it still active?" — confirm `bill_votes_tool_used: true` on turn 2.
2. **Multi-bill history**: turn 1 mentions HR 7147, turn 3 mentions HB 1234, turn 5 user "what's the status?" — confirm tool fires with the most recently mentioned bill (HB 1234) by checking the response references HB 1234, not HR 7147.
3. **No bill in conversation**: empty or non-bill conversation, current message "what's the status?" — confirm tool does NOT fire (falls through to existing low-confidence path).
4. **Bill mentioned ONLY in bot response, not in any user message**: turn 1 user "tell me about immigration bills", turn 2 bot mentions HR 7147 in its answer, turn 3 user "is it active?" — confirm tool does NOT fire (user-only filter rejects bot mentions). This is the false-positive case the user-mention dominance guard prevents.
4b. **Bill mentioned in user message AND bot response**: turn 1 user "tell me about HR 7147", turn 2 bot mentions HR 7147 + tangential HB 1234, turn 3 user "is it active?" — confirm tool fires for HR 7147 (the user mention) and not HB 1234 (bot-only).
5. **Bill mentioned beyond 6-turn window**: turn 1 mentions HR 7147, turns 2–7 unrelated, turn 8 "what's the status?" — confirm tool does NOT fire (consistent with `_prefetch_bill_info` Method 3 depth limit).
6. **Ambiguous/malformed reference**: turn 1 user "I'm interested in resolution 7147" (no bill prefix) → turn 2 "is it active?" — confirm tool does NOT fire (regex doesn't match, no false positive).
7. **Backward-compat**: call `_should_use_bill_votes_tool(rag_confidence, message, page_context)` without `conversation_history` argument — confirm function still works (default `None` path).

*Production validation:*

- Pull 7 days of logs after deploy and re-run the leak check (`'bill-history' in retrieval_sources`). Should be 0.
- **Add automated check to `scripts/evaluate_production.py`**: when generating the analytics report, surface a `bill_history_leak_count` metric. Any non-zero value flags a regression. This becomes a permanent canary on every future eval run, not a one-shot post-deploy check.
- Run the eval at days 7, 14, and 30 post-deploy as a sustained validation window. Bills sync runs spread across days; any latent producer path missed by F1 (e.g., a code path called only on backload mode) would surface within this window.

**Why not also patch the general-path retrieval directly?**

We considered adding `document_type: {"$ne": "bill-history"}` to the unfiltered queries at `retrieval.py:206-211` and `retrieval.py:572-583`. Decided against it: that's a band-aid that only addresses one document type. Removing the data at its source is permanent and self-enforcing — no future read path can ever surface bill-history because it doesn't exist.

Fixes B through F together ensure live, complete, and uncontaminated OpenStates data is used for any status query, regardless of page type or whether the bill identifier appears in the current message.

### Fix F production verification (2026-04-29)

Shipped in commits: votebot `698958e` + `c0c6c11` + `32150a6` + `a792808`; ddp-sync `3ea053f`. Deploy completed and validated 2026-04-29 ~02:47 UTC.

**Probe results** (single-bill sync via `/ddp-sync/v1/sync/unified`):
- `document_ids`: only `bill-webflow-...` returned. **No `bill-history-*` entries** ✓
- `chunks_created`: dropped from 57 → 45 per bill (the missing 12 chunks were the bill-history doc that's no longer produced)

**Pinecone flush results**:
- 1,103 stale bill-history vectors deleted via metadata filter
- Eventual-consistency retry caught 100 stragglers on first poll, count clean by second poll (3s later)
- Post-flush sync verified count stays at 0 — F1 producer is fully removed

**F3 / live-tool firing verified end-to-end**:
- A status query "what's the latest status?" on a bill page (HB 1D) fired the live OpenStates tool (`bill_votes_tool_used: true` in the logged event) and pulled actions dated 2026-04-28 — same-day data the live path provides
- The `_should_use_bill_votes_tool()` user-only conversation-history scan was exercised on multiple turns; no bot-only false positives observed

**Performance baseline** (from post-deploy logs):
- OpenStates round-trip is **~308ms** for a typical bill (per the new `bill_votes_tool_duration_ms` field). Not the latency bottleneck — a 21s outlier observed earlier in the day was attributable to LLM streaming variability, not the live tool.

### Deploy-day learnings (worth documenting for future rollouts)

1. **Non-editable install on ddp-sync.** First restart appeared to succeed (git pull was clean) but the running code was a cached `pip install .` snapshot from the original deploy, not the new source. Symptom: response `document_ids` still contained `bill-history-...` despite the source on disk being clean (`format_bill_history_chunk` already deleted). Fix during deploy: `.venv/bin/pip install -e .` then restart. Now editable; future deploys propagate without reinstall.

2. **VoteBot uses `PYTHONPATH=/home/ubuntu/votebot/src` in its systemd unit.** Source changes load on restart even though the install is non-editable, because PYTHONPATH wins over site-packages in module resolution. Less surprising than ddp-sync.

3. **DDP-Sync routes mount under `API_PREFIX = "/ddp-sync/v1"`** (see `src/ddp_sync/app.py:15`). The localhost URL is `http://localhost:8001/ddp-sync/v1/sync/unified`, not `/sync/unified` despite the README's path table implying otherwise (the table values are relative to the prefix). Public access via DDP-API proxy is `https://api.digitaldemocracyproject.org/votebot/sync/unified` (proxy strips its own prefix). The deploy script (`scripts/deploy_fix_f.sh`) now branches on the URL to pick the right path.

4. **Venv path conventions differ:** ddp-sync uses `~/ddp-sync/.venv/`; votebot uses `~/votebot/venv/` (no leading dot). Easy to trip on when copy-pasting one-liners.

### Observability follow-ups (2026-04-29, post-deploy)

Two small instrumentation gaps surfaced during validation:

1. `bill_votes_tool_used` was logged via "X if X else None" pattern in `query_logger.py`, which dropped False values during JSON serialization. The field was missing entirely from `query_processed` events, making the OpenStates fire-rate metric unmeasurable. Fixed in commit `3e3574b`: now logged as bool always (False/True).

2. Added new `bill_votes_tool_duration_ms` field. Without per-call timing, slow responses couldn't be diagnosed (a 21.3s status query in the first test was either OpenStates being slow or LLM streaming being slow — no way to tell). Now: streaming-path `_prefetch_bill_info` is wrapped with `time.perf_counter()` and the duration flows through `AgentResult` → `_log_query` → `query_logger`.

   Other "X if X else None" log fields (`web_search_used`, `fallback_used`, `handoff_triggered`, etc.) follow the same drop-when-False pattern but were left untouched in this fix to avoid disturbing existing log analysis tooling. Worth flipping in a future cleanup if Ramon decides to standardize.

   The non-streaming `process_message` path doesn't get instrumented in this commit because the tool fires inside the LLM's function-calling dispatch (in `services/llm.py`), not as a separate prefetch step. WebSocket traffic is the dominant production path so this covers the main visibility need.

---

## Implementation

### Phase 1: Backend — Stale Status Fix + Caching Service

**1a. Fix `_should_use_bill_votes_tool()` (Fix B above)**
- Add `page_context` parameter
- Add `is_bill_page_status_query` check
- Update both call sites
- **This ships independently and immediately fixes HR 7147**

**1b. `src/votebot/services/button_cache.py` (new)**
```python
class ButtonCache:
    """Redis-backed cache for quick-action button responses."""

    CACHEABLE_TYPES = ("summary", "pros_cons")  # status_votes is NEVER cached
    SAFETY_TTL = 604800  # 7-day safety net TTL

    def __init__(self, redis_store):
        self.redis = redis_store

    def _key(self, slug: str, button_type: str) -> str:
        return f"votebot:button:{slug}:{button_type}"

    async def get(self, slug: str, button_type: str) -> dict | None:
        """Get cached button response. Returns None on miss or non-cacheable type."""
        if button_type not in self.CACHEABLE_TYPES:
            return None
        # ...

    async def set(self, slug: str, button_type: str, response: dict) -> None:
        """Cache a button response. No-op for non-cacheable types."""
        if button_type not in self.CACHEABLE_TYPES:
            return
        # ... set with SAFETY_TTL ...

    async def invalidate_bill(self, slug: str) -> None:
        """Clear all cached button responses for a bill."""
        for bt in self.CACHEABLE_TYPES:
            await self.redis.delete(self._key(slug, bt))
```

**1c. `src/votebot/core/agent.py` — Button-aware processing**
- Detect `button` metadata in request
- For cacheable types: check cache first, populate on miss
- For status_votes: always run full pipeline with bill_votes tool
- Log `button_type` and `cache_hit` in query events

**1d. `src/votebot/services/query_logger.py` — Add logging fields**
- Add `button_type: str | None` and `cache_hit: bool | None` to `log_event()`

**1e. `src/votebot/config.py` — Feature flag**
- Add `quick_action_buttons_enabled: bool = False`

### Phase 2: Frontend — Chat Widget Buttons

**File:** `chat-widget/src/ui.js` — Render button bar

- Show buttons only on bill pages (`page_context.type === 'bill'`)
- Disappear after any button tap or user message
- Reappear on navigation to a different bill
- Accessible: keyboard nav, aria-labels, focus indicators

**File:** `chat-widget/src/chat.js` — Button click handler

- Construct message with bill context from page_context
- Include `button` type in WebSocket payload
- Display message in chat as if user typed it

**File:** `chat-widget/src/widget.js` — Button visibility logic and feature flag check

### Phase 3: Cache Invalidation from ddp-sync

**File:** `ddp-sync/src/ddp_sync/pipelines/bill_version.py` — After `_ingest_bill_text()` succeeds and `set_bill_version()` updates the version cache (~line 522), publish invalidation:

```python
# After successful Pinecone ingestion and version cache update
await redis_store.publish(
    "votebot:cache:invalidate",
    json.dumps({"slug": bill_slug, "reason": "bill_version_change", "version_note": version_note})
)
logger.info("Published button cache invalidation", slug=bill_slug, version_note=version_note)
```

**File:** `src/votebot/main.py` — On startup:
1. Run startup reconciliation: scan `votebot:button:*` keys, compare `cached_at` against `ddp:bill_version:*` `last_checked` timestamps, invalidate stale entries
2. Subscribe to `votebot:cache:invalidate` channel (leader worker only). On message, call `button_cache.invalidate_bill(slug)` and log the invalidation.

---

## Testing

### Backend
1. Unit test: cache hit returns stored response without LLM call
2. Unit test: cache miss triggers normal pipeline and caches result
3. Unit test: `status_votes` button type is never cached (always returns None from cache)
4. Unit test: cache invalidation clears all keys for a bill slug
5. Unit test: `_should_use_bill_votes_tool()` fires for "what's the latest action?" on bill pages (the HR 7147 regression test)
6. Unit test: feature flag disabled → button metadata ignored, cache not read/written
7. Unit test: startup reconciliation detects stale cache entries (cached_at < bill last_checked) and invalidates them
8. Integration test: bill page status query triggers live OpenStates

### Frontend
1. Buttons appear only on bill pages
2. Buttons disappear after first interaction
3. Button click sends correct message with button metadata
4. Buttons reappear on navigation to different bill
5. Keyboard navigation works (Tab, Enter/Space)
6. Screen reader announces button labels

### Production Validation
1. Check logs for `button_type` and `cache_hit` fields
2. Compare latency: cache hits <100ms vs. 8–13s for LLM
3. Verify status button returns fresh OpenStates data (cross-check with congress.gov)
4. Monitor Redis memory usage (expect ~4MB for button cache)

---

## Implementation Order

1. **Fixes B–E (stale status, partial)** — SHIPPED. Fix B: bill page status queries trigger tool (commit `52b8d80`). Fix C: resolve bill from Webflow slug (commit `ee1227a`). Fix D (PARTIAL — completed by Fix F): bill-page bill-history removal from retrieval (commit `b518132`). Fix E: pass full action history to LLM (commits `d0fed09`, `6f756c7`). See `docs/TROUBLESHOOTING.md` for full details.
2. **Fix F — Complete bill-history removal** — ✅ SHIPPED 2026-04-29. F3 + F1 + F2 all live and verified. See "Fix F production verification (2026-04-29)" subsection above for results. Validation cadence: re-run `evaluate_production.py` at days 7/14/30 from 2026-04-29 to confirm `bill_history_leak_count: 0` holds across fresh traffic.

   **Release sequencing — `scripts/deploy_fix_f.sh`** (or equivalent runbook).
   The F1 → F2 sequence is the highest-risk part of the rollout (window where a missed verification step would either leave stale chunks regenerating or delete before the producer is shut down). Codify the sequence in a single fail-fast script:
   ```bash
   #!/bin/bash
   set -euo pipefail
   # Step 1: Verify ddp-sync is on the new commit (no bill-history production)
   echo "Probing ddp-sync for bill-history production..."
   PROBE_RESPONSE=$(curl -sf -X POST "$DDP_SYNC_URL/votebot/v1/sync/unified" \
     -H "Authorization: Bearer $API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"content_type":"bill","mode":"single","slug":"<known-test-bill-slug>"}')
   if echo "$PROBE_RESPONSE" | grep -q "bill-history-"; then
     echo "FAIL: ddp-sync still producing bill-history docs. F1 not yet deployed?"
     exit 1
   fi
   echo "OK: ddp-sync probe clean."

   # Step 2: Run the flush
   echo "Running Pinecone flush..."
   PYTHONPATH=src .venv/bin/python scripts/flush_bill_history.py --confirm

   # Step 3: Post-flush sanity check
   POST_COUNT=$(jq -r '.post_count' logs/eval/flush_bill_history.json)
   if [ "$POST_COUNT" != "0" ]; then
     echo "FAIL: $POST_COUNT bill-history vectors remain after flush."
     exit 1
   fi
   echo "OK: Pinecone bill-history count is 0."
   ```
   Manual sequencing remains possible (Ramon's preferred ops style is SSH + manual command). The script is just a guardrail that turns "did I remember to verify ddp-sync first?" into a fail-fast exit code.
3. **Backend caching + logging + feature flag** — ✅ SHIPPED 2026-04-29 (votebot `e8cb33c` + `b000c03`). ButtonCache service (Redis-backed), agent button-aware processing, query_logger fields (`button_type`, `cache_hit`), `quick_action_buttons_enabled` flag, `/votebot/v1/features` discovery endpoint, `DELETE /votebot/v1/cache/button/{slug}` admin endpoint. Deployed with flag off — entire feature inert until Step 5.
4. **Frontend buttons** — ✅ SHIPPED 2026-04-29 (votebot `8fc9b53` + `b000c03`). Three buttons render below input on bill pages, fetch `/features` at init, hide-on-send + re-show on bill navigation, WCAG 2.1 AA (native `<button>`, aria-labels, focus indicators, focus-shift on hide). Minified bundle rebuilt. CloudFlare cache purge required at enablement.
5. **Cache invalidation from ddp-sync** — ✅ SHIPPED 2026-04-29 (ddp-sync `58956ba` + votebot `e8cb33c` / `b000c03`). DDP-Sync publishes `{slug, reason, version_note}` on `votebot:cache:invalidate` after successful bill text re-ingestion (`chunks_created > 0`). VoteBot subscribes on startup with auto-reconnect supervisor (exponential backoff 1s→60s). Startup reconciliation iterates `ddp:bill_version:*` records (using new `bill_slug` field) and invalidates entries with `cached_at` < `last_checked`. Subscriber runs on all workers — invalidation is idempotent, leader election was deemed over-engineered at 2-worker scale.
6. **Enable feature flag** — Manual step. Add `VOTEBOT_QUICK_ACTION_BUTTONS=true` to `~/votebot/.env`, restart `votebot.service`, purge CloudFlare cache for `ddp-chat.min.js`. Watch the eval canary at days 7/14/30 for `cache_hit` distribution and `bill_history_leak_count` (still must be 0 from Fix F).

Step 2 was independent of the buttons feature and shipped first; Step 5 was previously the cache-invalidation rollout, now bundled with Steps 3–4.

### Phase 2/3/5 production verification plan

To run after Step 6 (enabling the flag):

1. Open the chat widget on a known-good bill page (e.g., `/bills/one-big-beautiful-bill-act-hr1-2025`). Three buttons should render below the input.
2. Tap "Summarize this bill" — verify response appears, `query_processed` event in JSONL has `button_type: "summary"` and `cache_hit: false`.
3. Refresh the page, open the widget again, tap "Summarize this bill" — should now serve from cache. Event has `button_type: "summary"` and `cache_hit: true`. Total `duration_ms` should be near-instant (<100ms vs 8–13s for the cache-miss path).
4. Tap "Latest status & votes" twice in a row — both should fire the live tool (`bill_votes_tool_used: true`, `cache_hit` not applicable since status_votes is never cached).
5. Tap "Pros and cons" — repeats step 2/3 on a different cached key.
6. Trigger a real bill version change via DDP-Sync (or wait for the daily 04:00 UTC scheduler to find one). Verify the cache for that bill is cleared by tapping "Summarize" again — should fire a fresh LLM call (`cache_hit: false`).
7. Manual cache clear: `curl -X DELETE -H "Authorization: Bearer $API_KEY" https://api.digitaldemocracyproject.org/votebot/cache/button/{slug}` — should return `{"slug": ..., "deleted": 0..2}`. Steps 1–3 can deploy without frontend changes. Step 4 is a separate widget deploy. Step 5 is an env var change. Step 6 is a ddp-sync change.

### Rollback

- **Instant:** Set `VOTEBOT_QUICK_ACTION_BUTTONS=false` and restart. Buttons disappear, cache ignored.
- **Cache issues only:** Flush all button cache keys: `redis-cli KEYS "votebot:button:*" | xargs redis-cli DEL`
- **Widget issues only:** Revert widget JS and purge CloudFlare.

---

## Resolved Questions

- **Slug uniqueness** — Confirmed: Webflow slugs are unique and immutable. No composite keys needed.
- **Cache staleness** — Cache persists until ddp-sync detects a bill amendment. Bills no longer in session keep cached responses (correct — content is stable). 7-day TTL as safety net. Startup reconciliation catches missed pub/sub events.
- **OpenStates rate limits** — 30,000 API calls/day licensed. Current status query volume (~30/day) is well within limits, even with significant traffic growth.
- **OpenStates fallback** — Existing web search fallback in VoteBot handles OpenStates degradation. No additional SLO work needed.
- **Live data badge** — Rejected. Users care about accuracy, not whether a response is cached or live. Adding a "live" indicator on status responses adds UI complexity with no user value.

## Open Questions

1. **Button labels** — "Summarize this bill" vs. "What does this bill do?" vs. "Bill summary"? Recommend starting with the clearest action verb and iterating based on CTR data.
2. **Button placement** — Below the input? Above the first message? As clickable chips in the welcome message? Recommend above the first message (most visible before user engages).
