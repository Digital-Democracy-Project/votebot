# PLAN: User Analytics & Behavioral Logging

**Goal:** Transform query logging from a flat log file into a behavioral analytics system that supports visitor segmentation, outcome measurement, and product insight.

**Status:** Approved — ready for implementation

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

Before designing fields, define what decisions this enables.

### Metric Definitions

Metrics are defined at three levels. When reporting, always name the level explicitly to avoid conflicting numbers (e.g., "5% query handoff rate" vs "12% conversation handoff rate" can both be true).

| Metric | Level | Definition | Required Fields |
|---|---|---|---|
| **Query success rate** | Query | See "Success Tiers" below | `confidence`, `retrieval_count`, `fallback_used`, `handoff_triggered`, `has_citations` |
| **Retrieval miss rate** | Query | % of queries with `retrieval_count == 0` | `retrieval_count` |
| **Fallback rate** | Query | % of queries where `fallback_used == true` (not same as web search — see below) | `fallback_used`, `fallback_reason` |
| **Web search rate** | Query | % of queries where `web_search_used == true` (informational, not necessarily failure) | `web_search_used` |
| **Query handoff rate** | Query | % of queries that triggered handoff | `handoff_triggered` |
| **Conversation handoff rate** | Conversation | % of conversations containing at least one handoff | `conversation_id`, `handoff_triggered` |
| **Session handoff rate** | Session | % of sessions containing at least one handoff | `session_id`, `handoff_triggered` |
| **Avg queries per conversation** | Conversation | Mean `conversation_message_index` at conversation end | `conversation_id`, `conversation_message_index` |
| **Drop-off rate** | Conversation | % of conversations with only 1 query | `conversation_id`, `conversation_message_index` |
| **Unique visitors** | Visitor | Count of distinct `visitor_id` values | `visitor_id` |
| **Intent distribution** | Query | Breakdown by `primary_intent` and `sub_intent` | `primary_intent`, `sub_intent` |
| **Vote CTA exposure rate** | Session | % of sessions where a response contained a vote CTA link | Proxy for conversion — true conversion (click-through) *not measurable in v1*, requires widget click tracking. Do not label this as "conversion" in reports. |

### Success Tiers

"Query success" is not a single number. Report it as layered tiers:

| Tier | Definition | What It Measures |
|---|---|---|
| **System success** | Response returned, no error, no handoff triggered | "Did the system work?" |
| **Citation-grounded success** | `retrieval_count > 0` and `has_citations == true` | "Did we find and cite relevant data?" (Conservative proxy — a valid answer may exist without surfaced citations, e.g., tool-returned facts.) |
| **Heuristic answer success** | Confidence >= threshold + no fallback + no handoff + has citations | "Did we probably answer well?" |
| **User success** | Positive feedback, click-through, or conversion | "Did the user get what they needed?" — *Not measurable in v1.* |

### Fallback vs Web Search

These are distinct concepts:

| Field | Meaning |
|---|---|
| `web_search_used` | OpenAI web search tool was invoked. May be intentional (e.g., current events query) or a fallback. |
| `fallback_used` | The system fell back from its primary retrieval path because RAG was insufficient. |
| `fallback_reason` | Why fallback occurred: `no_internal_retrieval`, `low_confidence`, `out_of_scope`, `missing_bill_context` |

Web search can be a normal tool invocation, not necessarily a failure. Treating all web search as fallback inflates apparent failure rate.

---

## Design

### Three-Level Identity Model

| Level | ID | Storage | Lifetime | Purpose |
|---|---|---|---|---|
| **Visitor** | `visitor_id` | `localStorage` | Permanent (best-effort) | Cross-session device tracking |
| **Session** | `session_id` | `sessionStorage` (existing) | Per-tab, 30-min timeout | Visit-level intent |
| **Conversation** | `conversation_id` | Server-side | See rules below | Multi-turn behavior |

**Important caveat:** `visitor_id` tracks **browser instances**, not people. It is cleared by incognito mode, Safari ITP, storage clearing, and is not shared across devices or browsers. Treat it as best-effort device identity, not stable user identity.

### Conversation Boundary Rules

A new conversation starts when **any** of these occur:
1. Explicit new chat / session reset
2. Inactivity timeout (no message for 10+ minutes within a session)
3. Page type changes (e.g., bill → legislator, legislator → organization)
4. Page ID changes within the same type (e.g., bill HB-155 → bill SB-200), **but only if the previous conversation had at least one completed response**. This avoids splitting the user's initial exploration journey too aggressively.

These rules are deterministic and ordered by priority. The server tracks `last_message_time`, `last_page_context_type`, `last_page_context_id`, and `conversation_has_response` per session.

`conversation_id` format: `{session_id}:{n}` where `n` is the conversation counter within the session.

**Event sequencing:** Conversation boundary evaluation occurs **before** `message_received` is emitted. The processing order on each incoming message is:
1. Receive message
2. Evaluate conversation boundary rules
3. If boundary detected, emit `conversation_ended` for the previous conversation
4. Assign new or current `conversation_id`
5. Emit `message_received` with the final assigned `conversation_id`

This ensures each message is logged against its correct conversation.

**Note:** These boundaries are a v1 heuristic. They may overcount conversations in some edge cases (e.g., a user exploring related bills in quick succession). The rules should be evaluated against real log data after deployment and tuned if conversation metrics look anomalous.

### Message Indexing

Two counters, tracked separately:

| Counter | Scope | Resets When |
|---|---|---|
| `session_message_index` | Per session | Never (within session lifetime) |
| `conversation_message_index` | Per conversation | When `conversation_id` changes |

`conversation_message_index` is the primary field for turn-count and drop-off analysis. `session_message_index` is useful for session-level engagement depth.

### Event Types

Three event types in v1 — enough for a real event model without over-engineering:

| Event | When Emitted | Key Fields |
|---|---|---|
| `message_received` | Server receives a user message, before processing | `visitor_id`, `session_id`, `conversation_id`, `message`, `page_context`, `entry_referrer`, `page_url` |
| `query_processed` | Agent finishes processing and responds | All behavioral/outcome fields (intent, retrieval, confidence, fallback, etc.) |
| `conversation_ended` | Conversation boundary detected or session disconnects | Summary: `turn_count`, `duration_seconds`, `handoff_occurred`, `fallback_occurred`, `retrieval_miss_occurred`, `terminal_state`, `dominant_primary_intent` |

The `conversation_ended` event is a lightweight summary record that avoids reconstructing conversations from raw query records at analysis time.

`terminal_state` values:
- `"inactive_end"` — user stopped asking (inactivity timeout). Does **not** imply success — user may have been satisfied, confused, or distracted.
- `"handoff"` — escalated to human agent.
- `"abandoned"` — session disconnected mid-conversation (WebSocket close while streaming or within a turn). **Caution:** This is a transport-level proxy and may overstate true abandonment — users may close the tab after getting their answer, background the page, or lose connectivity briefly. Do not interpret as definitive dissatisfaction.
- `"navigated"` — user moved to a new page context, starting a new conversation.

`dominant_primary_intent` is the most frequent `primary_intent` observed in the conversation. Ties break to the first-seen intent.

### Intent Taxonomy

Two-level classification for actionable granularity:

| `primary_intent` | `sub_intent` examples |
|---|---|
| `bill` | `summary`, `support_opposition`, `vote_history`, `status`, `explanation`, `comparison` |
| `legislator` | `voting_record`, `contact`, `bio`, `ddp_score`, `sponsored_bills` |
| `organization` | `positions`, `info`, `bill_alignment` |
| `general` | `navigation`, `how_to_vote`, `about_ddp`, `issue_area` |
| `out_of_scope` | `greeting`, `off_topic`, `meta` |

`primary_intent` is classified via the existing keyword/regex approach (moved from `evaluate_production.py` into the agent). `sub_intent` uses a lightweight keyword match within the primary category. Both are best-effort heuristics, not ML classifiers.

**Implementation requirement:** Both `primary_intent` and `sub_intent` values must be defined as a central enum/constant list in the codebase. Do not add new values casually — taxonomy creep will degrade analytics consistency. New values require a deliberate decision.

### Event Schema: `query_processed`

```json
{
  "timestamp": "2026-03-26T15:35:53.460792+00:00",
  "event_type": "query_processed",

  "visitor_id": "v_a1b2c3d4e5f6",
  "session_id": "16e35df3-507",
  "conversation_id": "16e35df3-507:2",
  "session_message_index": 7,
  "conversation_message_index": 3,

  "message": "What organizations oppose this bill?",
  "response": "...",

  "primary_intent": "organization",
  "sub_intent": "support_opposition",
  "page_context": { "type": "bill", "id": "HB 155", ... },

  "confidence": 0.82,
  "retrieval_count": 6,
  "retrieval_sources": ["bill-text", "organization"],
  "has_citations": true,
  "citations_count": 2,
  "grounding_status": "grounded",
  "external_augmentation": "none",

  "web_search_used": false,
  "fallback_used": false,
  "fallback_reason": null,
  "bill_votes_tool_used": false,
  "handoff_triggered": false,
  "error": false,
  "error_type": null,

  "device_type": "mobile",
  "entry_referrer": "google.com",
  "page_url": "https://digitaldemocracyproject.org/bills/hb-155",

  "scroll_depth": 0.6,
  "time_on_page": 45,

  "channel": "websocket",
  "duration_ms": 4200,
  "citations": [...],
  "human_active": false,
  "client_ip": "...",      // debug/infra only — not for v1 analytics
  "user_agent": "..."
}
```

### Event Schema: `conversation_ended`

```json
{
  "timestamp": "2026-03-26T15:42:10.000000+00:00",
  "event_type": "conversation_ended",

  "visitor_id": "v_a1b2c3d4e5f6",
  "session_id": "16e35df3-507",
  "conversation_id": "16e35df3-507:2",

  "turn_count": 5,
  "duration_seconds": 372,
  "handoff_occurred": false,
  "fallback_occurred": true,
  "retrieval_miss_occurred": false,
  "terminal_state": "navigated",
  "primary_intents_seen": ["bill", "organization"],
  "dominant_primary_intent": "bill"  // most frequent; ties break to first seen
}
```

### Grounding & Augmentation

Two separate fields — grounding quality and external augmentation are independent dimensions:

**`grounding_status`** — How well the response is grounded in internal RAG data:

| Value | Condition |
|---|---|
| `"grounded"` | `retrieval_count > 0` and `has_citations == true` |
| `"partial"` | `retrieval_count > 0` but `has_citations == false` |
| `"ungrounded"` | `retrieval_count == 0` |

**`external_augmentation`** — Whether an external source supplemented the response:

| Value | Condition |
|---|---|
| `"none"` | No external source used |
| `"web"` | Web search was used (regardless of whether it was a fallback or intentional) |

A web-augmented answer can still be grounded, partially grounded, or ungrounded. These are orthogonal.

### Device Classification

Single `device_type` field derived server-side from `user_agent`:

| Value | Detection |
|---|---|
| `"desktop"` | Default (no mobile/tablet indicators) |
| `"mobile"` | UA contains "Mobile", "iPhone", "Android" (not tablet) |
| `"tablet"` | UA contains "iPad", "Tablet" |

The v1 `platform` field is removed — it overlapped with `device_type` and `channel`, producing contradictions (e.g., `platform: "web"` + `device_type: "mobile"`). `channel` (`websocket`/`rest`) already captures the transport. `device_type` captures the form factor.

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
        entry_referrer: isFirstMessage ? (extractDomain(document.referrer) || null) : undefined,
        page_url: window.location.href,
        scroll_depth: getScrollDepth(),
        time_on_page: getTimeOnPage()
    }
});
```

**No changes to widget.js, ui.js, or websocket connection logic.**

### Layer 2: Server — WebSocket Handler (`api/routes/websocket.py`)

**Session-level tracking:**

```python
sessions[session_id] = {
    # ... existing fields ...
    "conversation_counter": 0,
    "conversation_message_counter": 0,
    "session_message_counter": 0,
    "last_message_time": None,
    "last_page_context_type": None,
    "visitor_id": None,
    "conversation_intents": set(),
    "conversation_start_time": None,
    "conversation_had_handoff": False,
    "conversation_had_fallback": False,
    "conversation_had_retrieval_miss": False,
}
```

**Conversation boundary detection** — On each message, check:
1. Inactivity > 10 minutes since `last_message_time`
2. Page type changed (bill → legislator, etc.)
3. Same-type page changed (different bill slug/ID)

If boundary detected, emit `conversation_ended` for the previous conversation, then start a new one (increment counter, reset `conversation_message_counter` to 0).

**On WebSocket disconnect** — Emit `conversation_ended` for any active conversation.

**`handle_user_message()`** — Extract new fields, increment counters, derive `conversation_id`, emit `message_received` event, then forward to agent.

### Layer 3: Server — REST Chat Handler (`api/routes/chat.py`)

- Map `client_metadata.client_id` → `visitor_id`
- Add `entry_referrer` and `page_url` fields to `ClientMetadata` schema
- Read `navigation_context.scroll_depth` and `navigation_context.time_on_page` for logging

### Layer 4: Agent (`core/agent.py`)

**Intent classification** — Move `classify_query()` from `evaluate_production.py` into a shared util. Add `classify_sub_intent()` for the second level. Both are lightweight keyword/regex — no performance concern.

**Retrieval source extraction:**
```python
retrieval_sources = list({c.metadata.get("document_type") for c in retrieval_result.chunks if c.metadata})
```

**`retrieval_sources` must be normalized to the controlled vocabulary** defined in `MetadataExtractor` (`document_type` values: `bill`, `bill-text`, `bill-history`, `bill-votes`, `legislator`, `legislator-votes`, `organization`, `training`). Any unexpected values should be logged as warnings and mapped to `"unknown"` rather than passed through raw — inconsistent casing or naming (e.g., `"org"` vs `"organization"`) will produce noisy analytics.

**Grounding and augmentation derivation:**
```python
def _derive_grounding_status(retrieval_count, has_citations):
    if retrieval_count > 0 and has_citations:
        return "grounded"
    if retrieval_count > 0:
        return "partial"
    return "ungrounded"

def _derive_external_augmentation(web_search_used):
    return "web" if web_search_used else "none"
```

**Error handling** — If processing fails, `query_processed` is still emitted with `error: true`, `error_type` (e.g., `"llm_timeout"`, `"retrieval_error"`, `"internal_error"`), and `response: null`. This ensures system success rate is computable without cross-referencing `message_received` events.

**Fallback detection** — Distinguish `fallback_used` from `web_search_used`. Fallback is when web search was triggered specifically because RAG was insufficient (low confidence, no retrieval). Set `fallback_reason` accordingly.

**`_log_query()`** — Accept and forward all fields including the new behavioral data.

### Layer 5: Query Logger (`services/query_logger.py`)

**`log_event()`** — New method replacing `log_query()` (keep `log_query()` as a wrapper for backward compatibility):

```python
async def log_event(
    self,
    *,
    event_type: str,
    # Identity
    visitor_id: str | None = None,
    session_id: str,
    conversation_id: str | None = None,
    session_message_index: int | None = None,
    conversation_message_index: int | None = None,
    # Content (optional, not present on all event types)
    message: str | None = None,
    response: str | None = None,
    # Behavioral
    primary_intent: str | None = None,
    sub_intent: str | None = None,
    confidence: float | None = None,
    retrieval_count: int | None = None,
    retrieval_sources: list[str] | None = None,
    has_citations: bool | None = None,
    citations_count: int | None = None,
    grounding_status: str | None = None,
    external_augmentation: str | None = None,
    web_search_used: bool = False,
    fallback_used: bool = False,
    fallback_reason: str | None = None,
    bill_votes_tool_used: bool = False,
    handoff_triggered: bool = False,
    error: bool = False,
    error_type: str | None = None,
    # Conversation summary (for conversation_ended)
    turn_count: int | None = None,
    duration_seconds: int | None = None,
    handoff_occurred: bool | None = None,
    fallback_occurred: bool | None = None,
    retrieval_miss_occurred: bool | None = None,
    terminal_state: str | None = None,
    primary_intents_seen: list[str] | None = None,
    dominant_primary_intent: str | None = None,
    # Context
    page_context: dict | None = None,
    device_type: str | None = None,
    entry_referrer: str | None = None,
    page_url: str | None = None,
    scroll_depth: float | None = None,
    time_on_page: int | None = None,
    channel: str | None = None,
    duration_ms: int | None = None,
    citations: list[dict] | None = None,
    human_active: bool = False,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> None:
```

Device type derivation:
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

All events write to the same date-partitioned JSONL files. The `event_type` field distinguishes them.

### Layer 6: Evaluation Script (`scripts/evaluate_production.py`)

**New CLI flags:**
- `--visitor <visitor_id>` — filter to a specific visitor
- `--conversation` — group results by conversation
- `--event-type <type>` — filter by event type

**New report sections:**
- **Unique Visitors** and **Queries per Visitor**
- **Success Tiers** — system success, retrieval success, heuristic answer success rates
- **Intent Distribution** — by `primary_intent` and `sub_intent`
- **Fallback Analysis** — fallback rate by reason, distinct from web search rate
- **Conversation Metrics** — avg turn count, drop-off rate, duration distribution (from `conversation_ended` events)
- **Handoff Rates** — explicitly at query, conversation, and session levels
- **Behavioral Segments** — "visitors with >3 follow-ups", "visitors who trigger fallback", "visitors from bill pages"
- **Grounding Distribution** — % grounded vs partial vs ungrounded, cross-tabulated with `external_augmentation`

**Note:** `evaluate_production.py` is becoming both an evaluator and an analytics engine. It is a **reporting layer over logs, not the source of truth** — all core metric definitions and field derivations live in the logger and agent code, not in the report script. This is acceptable for v1, but if scope continues to grow, consider splitting into separate scripts (e.g., `analytics_report.py` for metrics/segments, `evaluate_production.py` for ground-truth validation).

### Layer 7: `client_ip` Fix

Separate infra task — configure nginx/ALB to set `X-Forwarded-For` properly.

---

## Data Flow Summary

```
Widget (browser)                     Server                           Log File
─────────────────                    ──────                           ────────
localStorage → visitor_id     ──►  websocket.py
document.referrer → entry_referrer ──►    evaluates conversation
location.href → page_url     ──►      boundary rules
scroll_depth, time_on_page    ──►    emits message_received     ──►  JSONL

                                    agent.py
                                      classifies intent (2-level)
                                      extracts retrieval_sources
                                      derives grounding_status
                                      determines fallback_used       JSONL
                                        + fallback_reason        ──► { event_type:
                                      reads AgentResult fields         "query_processed",
                                                                       ... }
user_agent header             ──►    derives device_type

                                    websocket.py
                                      on boundary / disconnect   ──► { event_type:
                                      emits conversation_ended         "conversation_ended",
                                                                       turn_count, ... }
```

---

## Data Governance

### Retention & Access

Raw `message` and `response` fields contain free text that may include personal information entered by users. Define:

- **Retention period:** 90 days for raw logs. After 90 days, logs are archived with `message` and `response` fields replaced by:
  - SHA-256 hash (for deduplication and matching)
  - A short auto-generated summary label (e.g., "bill summary request for HB-155") derived from `primary_intent`, `sub_intent`, and `page_context`
  - All structured fields (`primary_intent`, `confidence`, `grounding_status`, etc.) are preserved permanently
  This preserves long-term value for failure pattern analysis, prompt regression review, and retrieval gap analysis, while removing raw free text.
- **Access:** Raw logs accessible only to engineering and the analytics lead. Derived/aggregated reports (no raw text) available to broader team.
- **Derived fields:** For most analytics, use the structured fields rather than raw text. Raw text is for debugging and ground truth evaluation only.

### JSONL as Operational Store

JSONL is the operational source of truth for v1. Known limitations:
- Aggregation requires full file scans
- No joins across event types
- No indexing

Mitigation:
- `evaluate_production.py` generates periodic derived summary reports (JSON)
- Migration to columnar analytics (BigQuery / ClickHouse) is triggered by analysis pain, not volume alone
- The event schema is designed to map cleanly to a columnar table when that migration happens

---

## Backward Compatibility

- **Log format:** New fields are additive. Old log entries won't have them. All consumers must treat missing fields as `null`. New event types (`message_received`, `conversation_ended`) coexist with legacy `query_processed` entries.
- **Widget protocol:** The `user_message` payload gains optional fields. The server ignores unknown fields, so old widgets work with a new server and vice versa.
- **REST API:** `ClientMetadata` gains optional fields. Non-breaking.
- **No database migration** — logs are append-only JSONL files.

---

## Privacy Considerations

- `visitor_id` is a random opaque UUID — no PII. Cannot be reverse-mapped to a person.
- `entry_referrer` is truncated to domain only — no search query leakage. It is session-entry context: canonically populated on the first `message_received` of each session, `null` on subsequent events **by design** (not because the referrer is unknown). Analysts should not treat `null` as missing data.
- `client_ip` is already logged (even though it's broken). No new IP collection.
- `localStorage` can be cleared by the user at any time, resetting visitor identity.
- No cookies are introduced.
- Raw message/response text subject to retention policy (see Data Governance).

---

## Future Work (Out of Scope for v1)

These are explicitly deferred but the event schema is designed to accommodate them:

1. **Outcome events** — `vote_clicked`, `link_followed`, `feedback_submitted` event types. Requires widget-side click tracking. Until then, "conversion to vote" metric uses proxy (% of responses containing vote CTA link).
2. **User success tier** — Positive feedback, click-through, or conversion measurement. Requires feedback UI and event instrumentation.
3. **Feedback loop into RAG** — Use logged intent + success/failure to identify weak retrieval areas and improve prompts or ingestion.
4. **Columnar analytics backend** — Migrate to BigQuery or ClickHouse when JSONL aggregation becomes painful.
5. **Authenticated user ID** — If Webflow Memberships or another auth system is added, extend with an optional `user_id` field.
6. **Funnel reconstruction** — Build entry → interaction → outcome funnels from ordered events per session.
7. **ML-based intent classification** — Replace keyword heuristics with a lightweight classifier trained on logged data.

---

## Files Changed

| File | Change |
|---|---|
| `chat-widget/src/websocket.js` | Add `visitor_id` generation/persistence via localStorage, expose `getVisitorId()` |
| `chat-widget/src/chat.js` | Include `visitor_id`, `entry_referrer` (first message only), `page_url`, `scroll_depth`, `time_on_page` in message payload |
| `src/votebot/api/routes/websocket.py` | Conversation boundary detection, message indexing, emit `message_received` and `conversation_ended` events, extract new fields |
| `src/votebot/api/routes/chat.py` | Read `client_metadata` and `navigation_context` fields, pass to agent |
| `src/votebot/api/schemas/chat.py` | Add `entry_referrer`, `page_url` to `ClientMetadata` |
| `src/votebot/core/agent.py` | Two-level intent classification, retrieval source extraction, grounding status + external augmentation, fallback detection, error path in `query_processed`, thread all fields through `_log_query()` |
| `src/votebot/services/query_logger.py` | New `log_event()` method, `device_type` derivation, support for all three event types, `dominant_primary_intent` on `conversation_ended` |
| `scripts/evaluate_production.py` | Success tiers, fallback analysis, conversation metrics, behavioral segments, multi-level handoff rates |

---

## Estimated Scope

- **8 files** modified
- ~100 lines of new widget JS
- ~200 lines of new/modified Python across server files (conversation tracking + event emission is the largest addition)
- ~120 lines of new evaluation script code
- No new dependencies
- No infrastructure changes required
