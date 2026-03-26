# PLAN: User Analytics Logging

**Goal:** Add persistent visitor/user identity to query logs so production data can be segmented by user for analytics and evaluation.

**Status:** Draft — awaiting review

---

## Problem

Current query logs capture only ephemeral data for user identification:

| Field | Current Value | Usefulness for Segmentation |
|---|---|---|
| `session_id` | Random 12-char UUID (`16e35df3-507`) | Per-tab, lost on page reload or 30-min timeout |
| `client_ip` | Always `127.0.0.1` | Behind reverse proxy, useless |
| `user_agent` | Browser string | Shared across many users |

There is no field that persists across sessions or ties activity to an individual visitor. We cannot answer questions like "how many unique users?", "what does a returning user's session look like?", or "which users trigger handoff most?".

### What Already Exists But Isn't Wired Up

1. **`ClientMetadata` schema** (`api/schemas/chat.py:65-84`) — has `client_id`, `client_version`, `user_agent`, `platform` fields. Used in `ChatRequest` but never read by the chat endpoint or passed to the logger.

2. **`NavigationContext` schema** (`api/schemas/chat.py:46-63`) — has `previous_pages`, `time_on_page`, `scroll_depth`. Accepted by the REST endpoint and passed to the agent, but never logged.

3. **Widget config** (`DDPChatConfig`) — supports arbitrary config keys via `window.DDPChatConfig`, but currently only passes `wsUrl`, `primaryColor`, `botName`, `autoOpen`, `autoDetect`.

4. **Widget session storage** (`websocket.js`) — uses `sessionStorage` (per-tab, clears on close) with prefix `ddp_votebot_`. Stores `session_id`, `last_activity`, `page_context`, `popup_open`.

---

## Design

### Identity Model

We need two levels of identity:

| Level | Storage | Lifetime | Purpose |
|---|---|---|---|
| **`visitor_id`** | `localStorage` | Permanent (until cleared) | Cross-session visitor tracking. Same device, same browser = same visitor. |
| **`session_id`** | `sessionStorage` (existing) | Per-tab, 30-min timeout | Conversation continuity (already exists) |

**No authenticated user ID** is included in this plan. The DDP site uses Webflow hosting — if Webflow Memberships or another auth system is added later, we can extend by adding an optional `user_id` field. For now, `visitor_id` is the segmentation key.

### What Gets Logged

New fields added to each JSONL log entry:

```json
{
  "visitor_id": "v_a1b2c3d4e5f6",
  "platform": "web",
  "device_type": "mobile",
  "referrer": "https://google.com/...",
  "page_url": "https://digitaldemocracyproject.org/bills/hb-123"
}
```

| Field | Source | Notes |
|---|---|---|
| `visitor_id` | Widget generates, sends in payload | localStorage-persisted UUID, prefixed `v_` |
| `platform` | Widget sends based on detection | `"web"` or `"mobile-web"` (viewport heuristic) |
| `device_type` | Server parses from `user_agent` | `"desktop"`, `"mobile"`, `"tablet"` |
| `referrer` | Widget reads `document.referrer` | First message per session only, or on context change |
| `page_url` | Widget reads `window.location.href` | Already partially captured via `page_context.url`, but not always set |

---

## Changes Required

### Layer 1: Chat Widget (`chat-widget/src/`)

**websocket.js** — Visitor ID generation and persistence

- Add `localStorage`-based `visitor_id` (generate UUID on first visit, reuse forever)
- Expose `getVisitorId()` on the `DDPWebSocket` module

```js
// localStorage for cross-session persistence (distinct from sessionStorage session_id)
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

**chat.js** — Include visitor metadata in `user_message` payload

- On `sendMessage()`, augment payload with `visitor_id`, `platform`, `referrer`, `page_url`
- `referrer` sent only on first message per session (avoid redundancy)

```js
// In sendMessage():
const sent = DDPWebSocket.send({
    type: 'user_message',
    payload: {
        message: message,
        page_context: pageContext,
        visitor_id: DDPWebSocket.getVisitorId(),
        platform: detectPlatform(),
        referrer: isFirstMessage ? document.referrer || null : undefined,
        page_url: window.location.href
    }
});
```

**No changes to widget.js, ui.js, or websocket connection logic.** The visitor data rides on the existing `user_message` payload — no new message types or connection params needed.

### Layer 2: Server — WebSocket Handler (`api/routes/websocket.py`)

**`handle_user_message()`** — Extract new fields from payload and forward to agent

```python
# In handle_user_message():
visitor_id = payload.get("visitor_id")
platform = payload.get("platform")
referrer = payload.get("referrer")
page_url = payload.get("page_url")
```

Pass these through to `agent.process_message_stream()` and ultimately to `_log_query()`.

**`ConnectionManager.connect()`** — Optionally store `visitor_id` in the session dict for use in Slack handoff context.

### Layer 3: Server — REST Chat Handler (`api/routes/chat.py`)

**`chat()` and `chat_stream()`** — Read from the existing `client_metadata` field on `ChatRequest`

- Map `client_metadata.client_id` → `visitor_id`
- Map `client_metadata.platform` → `platform`
- Add `referrer` and `page_url` fields to `ClientMetadata` schema

This keeps the REST path consistent without a breaking schema change — `client_id` just gets a well-defined meaning.

### Layer 4: Agent (`core/agent.py`)

**`_log_query()`** — Accept and forward new fields

```python
def _log_query(
    self,
    *,
    session_id: str,
    message: str,
    result: AgentResult,
    page_context: PageContext,
    channel: str,
    start_time: float,
    human_active: bool = False,
    client_ip: str | None = None,
    user_agent: str | None = None,
    # New fields:
    visitor_id: str | None = None,
    platform: str | None = None,
    referrer: str | None = None,
    page_url: str | None = None,
) -> None:
```

**`process_message()` and `process_message_stream()`** — Accept and pass through the new kwargs.

### Layer 5: Query Logger (`services/query_logger.py`)

**`log_query()`** — Accept and write new fields

```python
async def log_query(
    self,
    *,
    # ... existing params ...
    visitor_id: str | None = None,
    platform: str | None = None,
    device_type: str | None = None,
    referrer: str | None = None,
    page_url: str | None = None,
) -> None:
```

Add to the JSONL entry dict. `device_type` is derived server-side from `user_agent` using a simple heuristic (check for "Mobile", "Tablet", "iPad" substrings).

### Layer 6: Evaluation Script (`scripts/evaluate_production.py`)

**`load_log_entries()`** — No changes needed (it loads arbitrary JSON fields).

Add new CLI flags and report sections:

- `--visitor <visitor_id>` — filter to a specific visitor
- Add "Unique Visitors" and "Queries per Visitor" to the report summary
- Add "Results by Platform" and "Results by Device Type" sections

### Layer 7: `client_ip` Fix

The current `client_ip` is always `127.0.0.1` because the app is behind a reverse proxy. This is already being read from `X-Forwarded-For`, but the proxy may not be setting it. Two options:

- **Option A:** Configure nginx/ALB to set `X-Forwarded-For` (infrastructure change, outside this PR)
- **Option B:** Add FastAPI's `TrustedHostMiddleware` or use `X-Real-IP` as fallback

Recommend Option A as a separate task — it's an infra change, not a code change.

---

## Data Flow Summary

```
Widget (browser)                    Server                          Log File
─────────────────                   ──────                          ────────
localStorage → visitor_id    ──►  websocket.py
document.referrer → referrer ──►  extracts from payload
location.href → page_url    ──►  passes to agent
viewport → platform         ──►
                                   agent._log_query()     ──►   2026-03-26.jsonl
user_agent header           ──►  derives device_type    ──►   { visitor_id, platform,
                                                                  device_type, referrer,
                                                                  page_url, ... }
```

---

## Backward Compatibility

- **Log format:** New fields are additive. Old log entries simply won't have them. The evaluation script and any downstream consumers should treat missing fields as `null`.
- **Widget protocol:** The `user_message` payload gains optional fields. The server ignores unknown fields, so old widgets work fine with a new server and vice versa.
- **REST API:** `ClientMetadata` gains two optional fields (`referrer`, `page_url`). Non-breaking for existing clients.
- **No database migration** — logs are append-only JSONL files.

---

## Privacy Considerations

- `visitor_id` is a random opaque UUID — no PII. Cannot be reverse-mapped to a person.
- `referrer` may contain search queries (e.g., Google `?q=...`). Consider truncating to domain-only if this is a concern.
- `client_ip` is already logged (even though it's broken). No new IP collection.
- `localStorage` can be cleared by the user at any time, effectively resetting their visitor identity.
- No cookies are introduced — this is purely `localStorage`.

---

## Files Changed

| File | Change |
|---|---|
| `chat-widget/src/websocket.js` | Add `visitor_id` generation/persistence, expose `getVisitorId()` |
| `chat-widget/src/chat.js` | Include `visitor_id`, `platform`, `referrer`, `page_url` in message payload |
| `src/votebot/api/routes/websocket.py` | Extract new fields from payload, pass to agent |
| `src/votebot/api/routes/chat.py` | Read `client_metadata` fields, pass to agent |
| `src/votebot/api/schemas/chat.py` | Add `referrer`, `page_url` to `ClientMetadata` |
| `src/votebot/core/agent.py` | Thread new fields through `_log_query()`, `process_message()`, `process_message_stream()` |
| `src/votebot/services/query_logger.py` | Accept and write new fields, add `device_type` derivation |
| `scripts/evaluate_production.py` | Add `--visitor` filter, visitor/platform/device report sections |

---

## Estimated Scope

- **8 files** modified
- ~80 lines of new widget JS
- ~60 lines of new/modified Python across server files
- ~40 lines of new evaluation script code
- No new dependencies
- No infrastructure changes required

---

## Open Questions

1. **Should `referrer` be logged as full URL or truncated to domain?** Full URL is more useful for understanding traffic sources but may contain search query PII.
2. **Should we add a `visitor_id` to the WebSocket connection URL** (like `session_id`) for server-side session correlation, or is payload-level sufficient?
3. **Do we want to backfill `visitor_id` onto existing logs** by grouping on `session_id` + `user_agent`? This would give partial segmentation for historical data.
4. **Is the Webflow site planning to add user authentication?** If so, we should reserve a `user_id` field now even if it's always null initially.
