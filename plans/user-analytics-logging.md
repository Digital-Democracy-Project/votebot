# PLAN: User Analytics & Behavioral Logging

**Goal:** Transform query logging from a flat log file into a behavioral analytics system that supports visitor segmentation, outcome measurement, and product insight.

**Status:** Draft v2 — revised based on feedback

---

## Problem

Current query logs capture only a flat record per query with ephemeral identity:

| Field | Current Value | Usefulness |
|---|---|---|
| `session_id` | Random 12-char UUID (`16e35df3-507`) | Per-tab, lost on page reload or 30-min timeout |
| `client_ip` | Always `127.0.0.1` | Behind reverse proxy, useless |
| `user_agent` | Browser string | Shared across many users |

We cannot answer:
- "How many unique visitors use VoteBot?"
- "What % of queries are successfully answered?"
- "Which query types fail most?"
- "What drives users to drop off or trigger handoff?"
- "What does a multi-turn conversation look like?"

### What Already Exists But Isn't Wired Up

1. **`ClientMetadata` schema** (`api/schemas/chat.py:65-84`) — has `client_id`, `client_version`, `user_agent`, `platform` fields. Used in `ChatRequest` but never read by the chat endpoint or passed to the logger.

2. **`NavigationContext` schema** (`api/schemas/chat.py:46-63`) — has `previous_pages`, `time_on_page`, `scroll_depth`. Accepted by the REST endpoint and passed to the agent, but never logged. These are high-signal features for intent detection and engagement scoring.

3. **`AgentResult` dataclass** (`core/agent.py:36-50`) — already contains `web_search_used`, `bill_votes_tool_used`, `requires_human`, `retrieval_count`, `confidence`, `tokens_used`. All computed but only partially logged.

4. **Widget session storage** (`websocket.js`) — uses `sessionStorage` (per-tab, clears on close) with prefix `ddp_votebot_`. Stores `session_id`, `last_activity`, `page_context`, `popup_open`.

---

## Core Metrics

Before designing fields, define what decisions this enables. We must be able to compute:

| Metric | What It Tells Us | Required Fields |
|---|---|---|
| **Query success rate** | % of queries answered with confidence >= 0.5 | `confidence`, `intent` |
| **Fallback rate** | % of queries requiring web search | `web_search_used` |
| **Handoff rate** | % of sessions escalated to human | `handoff_triggered` |
| **Avg queries per conversation** | User engagement depth | `conversation_id`, `message_index` |
| **Drop-off rate** | % of conversations abandoned after 1 query | `conversation_id`, `message_index` |
| **Retrieval miss rate** | % of queries with 0 RAG results | `retrieval_count` |
| **Unique visitors** | Reach / adoption | `visitor_id` |
| **Queries per visitor** | Power user identification | `visitor_id` |
| **Intent distribution** | What users actually ask about | `intent` |
| **Conversion to vote** | Whether VoteBot drives civic action | `vote_clicked` (future) |

If we can't compute these from the logs, the logging is wrong.

---

## Design

### Three-Level Identity Model

| Level | ID | Storage | Lifetime | Purpose |
|---|---|---|---|---|
| **Visitor** | `visitor_id` | `localStorage` | Permanent (best-effort) | Cross-session device tracking |
| **Session** | `session_id` | `sessionStorage` (existing) | Per-tab, 30-min timeout | Visit-level intent |
| **Conversation** | `conversation_id` | Server-side (per page context) | Resets on page navigation | Multi-turn behavior |

**Important caveat:** `visitor_id` tracks **browser instances**, not people. It is cleared by incognito mode, Safari ITP, storage clearing, and is not shared across devices or browsers. Treat it as best-effort device identity, not stable user identity. If authenticated user IDs become available (Webflow Memberships, etc.), add an optional `user_id` field.

**`conversation_id`** is new and critical. It's derived from `session_id` + a counter that increments when the user navigates to a new page (context change). This lets us measure conversation length, multi-turn resolution, and drop-off within a single page visit.

### Event-Oriented Log Entry

Each log entry becomes a richer event with identity, behavior, and outcome fields:

```json
{
  "timestamp": "2026-03-26T15:35:53.460792+00:00",
  "event_type": "query_processed",

  "visitor_id": "v_a1b2c3d4e5f6",
  "session_id": "16e35df3-507",
  "conversation_id": "16e35df3-507:2",
  "message_index": 3,

  "message": "What organizations oppose this bill?",
  "response": "...",

  "intent": "organization",
  "page_context": { "type": "bill", "id": "HB 155", ... },

  "confidence": 0.82,
  "retrieval_count": 6,
  "retrieval_sources": ["bill-text", "organization"],
  "web_search_used": false,
  "bill_votes_tool_used": false,
  "handoff_triggered": false,

  "platform": "web",
  "device_type": "mobile",
  "referrer": "google.com",
  "page_url": "https://digitaldemocracyproject.org/bills/hb-155",

  "scroll_depth": 0.6,
  "time_on_page": 45,

  "channel": "websocket",
  "duration_ms": 4200,
  "citations": [...],
  "human_active": false,
  "client_ip": "...",
  "user_agent": "..."
}
```

### New Fields Breakdown

#### Identity fields (from widget)

| Field | Source | Notes |
|---|---|---|
| `visitor_id` | Widget generates, sends in payload | localStorage-persisted UUID, prefixed `v_`. Best-effort device identity. |
| `conversation_id` | Server derives from `session_id` + context change counter | Resets when page context changes. Format: `{session_id}:{n}` |
| `message_index` | Server increments per conversation | 1-indexed position within the conversation |

#### Behavioral fields (already computed by agent, just not logged)

| Field | Source | Notes |
|---|---|---|
| `intent` | `classify_query()` result (from `evaluate_production.py`, moved server-side) | `"bill"`, `"legislator"`, `"organization"`, `"general"`, `"out_of_scope"` |
| `retrieval_sources` | Document types from `RetrievalResult.chunks` metadata | e.g., `["bill-text", "bill-votes", "organization"]` |
| `web_search_used` | `AgentResult.web_search_used` | Already computed, not logged |
| `bill_votes_tool_used` | `AgentResult.bill_votes_tool_used` | Already computed, not logged |
| `handoff_triggered` | `AgentResult.requires_human` | Already computed, not logged |
| `retrieval_count` | `AgentResult.retrieval_count` | Already computed, not logged |

#### Engagement fields (from widget/NavigationContext)

| Field | Source | Notes |
|---|---|---|
| `platform` | Widget detects from viewport | `"web"` or `"mobile-web"` |
| `device_type` | Server derives from `user_agent` | `"desktop"`, `"mobile"`, `"tablet"` |
| `referrer` | Widget reads `document.referrer` | Truncated to domain only (privacy). First message per session only. |
| `page_url` | Widget reads `window.location.href` | Full URL of current page |
| `scroll_depth` | `NavigationContext.scroll_depth` | 0-1, already in schema but not logged |
| `time_on_page` | `NavigationContext.time_on_page` | Seconds, already in schema but not logged |

#### Event metadata

| Field | Source | Notes |
|---|---|---|
| `event_type` | Hardcoded per event | `"query_processed"` for now; future: `"handoff_initiated"`, `"handoff_resolved"`, `"feedback_submitted"` |

---

## Changes Required

### Layer 1: Chat Widget (`chat-widget/src/`)

**websocket.js** — Visitor ID generation and persistence

- Add `localStorage`-based `visitor_id` (generate UUID on first visit, reuse forever)
- Expose `getVisitorId()` on the `DDPWebSocket` module

```js
const VISITOR_KEY = 'ddp_votebot_visitor_id';

function _getOrCreateVisitorId() {
    try {
        let vid = localStorage.getItem(VISITOR_KEY);
        if (!vid) {
            vid = 'v_' + crypto.randomUUID().replace(/-/g, '').slice(0, 12);
            localStorage.setItem(VISITOR_KEY, vid);
        }
        return vid;
    } catch (e) {
        return null;  // Private browsing or storage disabled
    }
}
```

**chat.js** — Include visitor metadata and engagement data in `user_message` payload

```js
// In sendMessage():
const sent = DDPWebSocket.send({
    type: 'user_message',
    payload: {
        message: message,
        page_context: pageContext,
        visitor_id: DDPWebSocket.getVisitorId(),
        platform: detectPlatform(),
        referrer: isFirstMessage ? (extractDomain(document.referrer) || null) : undefined,
        page_url: window.location.href,
        scroll_depth: getScrollDepth(),
        time_on_page: getTimeOnPage()
    }
});
```

**No changes to widget.js, ui.js, or websocket connection logic.** The visitor data rides on the existing `user_message` payload.

### Layer 2: Server — WebSocket Handler (`api/routes/websocket.py`)

**Session-level conversation tracking:**

```python
# In ConnectionManager, add to session init:
sessions[session_id] = {
    # ... existing fields ...
    "conversation_counter": 0,
    "message_counter": 0,
    "visitor_id": None,
}
```

**`handle_context_update()`** — Increment conversation counter on page navigation.

**`handle_user_message()`** — Extract new fields from payload, increment message counter, derive `conversation_id`, forward to agent:

```python
visitor_id = payload.get("visitor_id")
session["visitor_id"] = visitor_id or session.get("visitor_id")
session["message_counter"] += 1

conversation_id = f"{session_id}:{session['conversation_counter']}"
message_index = session["message_counter"]
```

### Layer 3: Server — REST Chat Handler (`api/routes/chat.py`)

- Map `client_metadata.client_id` → `visitor_id`
- Map `client_metadata.platform` → `platform`
- Add `referrer` and `page_url` fields to `ClientMetadata` schema
- Read `navigation_context.scroll_depth` and `navigation_context.time_on_page` for logging

### Layer 4: Agent (`core/agent.py`)

**`_log_query()`** — Accept and forward all new fields. Also log behavioral data that's already computed but currently discarded:

```python
def _log_query(
    self,
    *,
    # ... existing params ...
    # Identity:
    visitor_id: str | None = None,
    conversation_id: str | None = None,
    message_index: int | None = None,
    # Behavioral (from AgentResult — already available):
    intent: str | None = None,
    retrieval_sources: list[str] | None = None,
    web_search_used: bool = False,
    bill_votes_tool_used: bool = False,
    handoff_triggered: bool = False,
    # Engagement:
    platform: str | None = None,
    referrer: str | None = None,
    page_url: str | None = None,
    scroll_depth: float | None = None,
    time_on_page: int | None = None,
) -> None:
```

**Intent classification** — Move the `classify_query()` logic from `evaluate_production.py` into the agent (or a shared util) so intent is classified at query time rather than only during offline evaluation. This is a lightweight regex/keyword check — no performance concern.

**Retrieval sources** — Extract unique `document_type` values from `RetrievalResult.chunks`:

```python
retrieval_sources = list({c.metadata.get("document_type") for c in retrieval_result.chunks if c.metadata})
```

### Layer 5: Query Logger (`services/query_logger.py`)

**`log_query()`** — Accept all new fields and write to JSONL entry. Add `device_type` derivation from `user_agent`:

```python
def _derive_device_type(user_agent: str | None) -> str:
    if not user_agent:
        return "unknown"
    ua = user_agent.lower()
    if "ipad" in ua or "tablet" in ua:
        return "tablet"
    if "mobile" in ua or "iphone" in ua or "android" in ua:
        return "mobile"
    return "desktop"
```

Add `event_type` field (hardcoded to `"query_processed"` for now — prepares for future event types).

### Layer 6: Evaluation Script (`scripts/evaluate_production.py`)

**New CLI flags:**
- `--visitor <visitor_id>` — filter to a specific visitor
- `--conversation` — group results by conversation

**New report sections:**
- **Unique Visitors** and **Queries per Visitor** summary
- **Results by Intent** — success rate per intent type
- **Results by Platform / Device Type**
- **Conversation Metrics** — avg length, drop-off rate (1-query conversations)
- **Behavioral Segments** — "users who ask >3 follow-ups", "users who trigger fallback", "users from bill pages"
- **Outcome Metrics** — fallback rate, handoff rate, retrieval miss rate

### Layer 7: `client_ip` Fix

Separate infra task — configure nginx/ALB to set `X-Forwarded-For` properly.

---

## Data Flow Summary

```
Widget (browser)                     Server                           Log File
─────────────────                    ──────                           ────────
localStorage → visitor_id     ──►  websocket.py
document.referrer → referrer  ──►    extracts from payload
location.href → page_url     ──►    derives conversation_id
viewport → platform           ──►    increments message_index
scroll_depth, time_on_page    ──►

                                    agent.py
                                      classifies intent
                                      extracts retrieval_sources       2026-03-26.jsonl
                                      reads web_search_used,      ──► { event_type,
                                        bill_votes_tool_used,           visitor_id,
                                        handoff_triggered               conversation_id,
                                      from AgentResult                  intent,
                                                                        retrieval_sources,
user_agent header             ──►    derives device_type          ──►   web_search_used,
                                                                        ... }
```

---

## Backward Compatibility

- **Log format:** New fields are additive. Old log entries won't have them. The evaluation script and any downstream consumers must treat missing fields as `null`.
- **Widget protocol:** The `user_message` payload gains optional fields. The server ignores unknown fields, so old widgets work with a new server and vice versa.
- **REST API:** `ClientMetadata` gains optional fields. Non-breaking.
- **No database migration** — logs are append-only JSONL files.

---

## Privacy Considerations

- `visitor_id` is a random opaque UUID — no PII. Cannot be reverse-mapped to a person.
- `referrer` is truncated to domain only — no search query leakage.
- `client_ip` is already logged (even though it's broken). No new IP collection.
- `localStorage` can be cleared by the user at any time, resetting visitor identity.
- No cookies are introduced.

---

## Future Work (Out of Scope for v1)

These are explicitly deferred but the event-oriented log format is designed to accommodate them:

1. **Outcome events** — `vote_clicked`, `link_followed`, `feedback_submitted` event types. Requires widget-side click tracking.
2. **Feedback loop into RAG** — Use logged intent + success/failure to identify weak retrieval areas and improve prompts or ingestion.
3. **Columnar analytics backend** — When JSONL aggregation becomes painful, migrate to BigQuery or ClickHouse. The event schema is designed to map cleanly to a columnar table.
4. **Authenticated user ID** — If Webflow Memberships or another auth system is added, extend with an optional `user_id` field.
5. **Funnel reconstruction** — Build entry → interaction → outcome funnels from ordered events per session.

---

## Files Changed

| File | Change |
|---|---|
| `chat-widget/src/websocket.js` | Add `visitor_id` generation/persistence via localStorage, expose `getVisitorId()` |
| `chat-widget/src/chat.js` | Include `visitor_id`, `platform`, `referrer`, `page_url`, `scroll_depth`, `time_on_page` in message payload |
| `src/votebot/api/routes/websocket.py` | Extract new fields from payload, track `conversation_id` and `message_index` in session, pass to agent |
| `src/votebot/api/routes/chat.py` | Read `client_metadata` and `navigation_context` fields, pass to agent |
| `src/votebot/api/schemas/chat.py` | Add `referrer`, `page_url` to `ClientMetadata` |
| `src/votebot/core/agent.py` | Add intent classification, extract retrieval sources, thread all new fields through `_log_query()` |
| `src/votebot/services/query_logger.py` | Accept and write all new fields, add `device_type` derivation, add `event_type` |
| `scripts/evaluate_production.py` | Add `--visitor`/`--conversation` filters, behavioral segment reporting, outcome metrics |

---

## Estimated Scope

- **8 files** modified
- ~100 lines of new widget JS
- ~120 lines of new/modified Python across server files
- ~80 lines of new evaluation script code
- No new dependencies
- No infrastructure changes required
