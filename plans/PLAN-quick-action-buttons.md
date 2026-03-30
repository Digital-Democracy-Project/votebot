# PLAN: Quick-Action Buttons with Response Caching

**Date:** 2026-03-30
**Status:** Draft v5 — final

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

Fixes B and C together ensure live OpenStates data is fetched for any status query on a bill page, regardless of whether the frontend passes `id`, `jurisdiction`, or just `slug`.

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

1. **Fixes B + C (stale status)** — SHIPPED. Fix B: `_should_use_bill_votes_tool()` fires on bill page status queries (commit `52b8d80`). Fix C: `_prefetch_bill_info()` resolves bill identifier from Webflow slug via `get_bill_details()` (commit `ee1227a`).
2. **Backend caching + logging + feature flag** — ButtonCache, agent changes, logger fields. Deploy with flag off.
3. **Frontend buttons** — Widget UI, handlers, accessibility. Deploy with CloudFlare purge.
4. **Enable feature flag** — Set `VOTEBOT_QUICK_ACTION_BUTTONS=true`, restart.
5. **Cache invalidation from ddp-sync** — Publish on bill update, subscribe in VoteBot.

Steps 1–2 can deploy without frontend changes. Step 3 is a separate widget deploy. Step 4 is an env var change. Step 5 is a ddp-sync change.

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
