# User Personalization Plan: Anonymous-to-Authenticated Identity Pipeline

## Problem Statement

VoteBot currently has **zero user identity**. Every chat session is an ephemeral UUID stored in `sessionStorage` (tab-scoped, 30-minute timeout, lost on tab close). We can't distinguish users, track return visits, or build persistent profiles. The production logs show `client_ip: 127.0.0.1` for all 278 queries last week because nginx/DDP-API strip the real IP. We suspect 91% of recent traffic is a single power user, but we literally can't prove it.

This plan creates a **progressive identity pipeline**: anonymous visitor tracking from day one, with a clean upgrade path to Memberstack-authenticated profiles. It bridges the gap between "we know nothing about our users" and the Polis/Jigsaw opinion elicitation system (see `PLAN-votebot-polis-jigsaw.md`) that fundamentally requires persistent identity to build opinion vectors across sessions.

---

## Goals

1. **Track anonymous visitors** across sessions, tabs, and return visits with a persistent visitor ID
2. **Capture behavioral signals** (pages viewed, topics engaged with, session patterns) before any login exists
3. **Provide a clean auth upgrade path** to Memberstack login when the user chooses to authenticate
4. **Merge anonymous activity** into the authenticated profile seamlessly (no data loss on login)
5. **Lay the foundation** for persistent opinion vectors needed by the Polis/Jigsaw integration
6. **Fix the IP blind spot** immediately (X-Forwarded-For propagation)

---

## Architecture Decision: Why Not Firebase?

Firebase Authentication is a reasonable choice for many apps, but it's **not the right fit here** for several reasons:

| Factor | Firebase Auth | Memberstack | Recommendation |
|--------|--------------|-------------|----------------|
| **Webflow integration** | None native; requires custom code | First-class Webflow integration (drop-in) | Memberstack |
| **User sees login on** | Custom UI you build | DDP website (Webflow-native modal) | Memberstack |
| **Session persistence** | Firebase SDK manages tokens | Memberstack JS SDK manages cookies/tokens | Memberstack |
| **User data storage** | Firestore (separate from Webflow) | Memberstack members + Webflow CMS | Memberstack |
| **Cost at scale** | Free tier generous, then pay-per-auth | Per-member pricing (~$25-50/mo for 1K members) | Comparable |
| **Anonymous-to-auth merge** | Possible but requires custom Firestore logic | Possible with webhook + metadata API | Both workable |
| **Polis XID mapping** | Custom mapping needed | Custom mapping needed | Same either way |
| **Existing DDP infra** | None | DDP already uses Webflow; Memberstack is the natural auth layer | Memberstack |

**Recommendation: Memberstack** for authentication, with a **lightweight anonymous tracking layer** we build ourselves using `localStorage` + Redis. Firebase adds infrastructure complexity (Firestore, Firebase Admin SDK, service accounts) without solving problems Memberstack doesn't already solve better in the Webflow ecosystem.

For **anonymous visitor tracking** (the pre-auth layer), we don't need Firebase Analytics or Firebase Anonymous Auth either. A simple `localStorage` visitor ID + Redis-backed profile is cheaper, simpler, and gives us full control over the data model.

### What About Firebase for Analytics Only?

Google Analytics / Firebase Analytics could supplement our query logging, but:
- We already log every query with latency, confidence, citations, and page context
- Adding a GA/Firebase pixel creates a third-party data dependency and privacy considerations
- Our JSONL logs give us raw data; analytics dashboards are a presentation problem, not a data problem

If we want dashboards later, Posthog (self-hosted) or a simple Grafana + our JSONL logs is more aligned with the DDP ethos of data sovereignty.

---

## Design: Three-Layer Identity Model

```
Layer 1: Visitor (anonymous)      Layer 2: Session (ephemeral)      Layer 3: Member (authenticated)
+----------------------------+    +----------------------------+    +----------------------------+
| visitor_id (localStorage)  |    | session_id (sessionStorage)|    | memberstack_id             |
| Persists across sessions   |    | Per-tab, 30-min timeout    |    | Permanent, cross-device    |
| Created on first widget    |    | Already exists today       |    | Created on Memberstack     |
| load, never expires        |    | Links to visitor_id        |    | login/signup               |
|                            |    |                            |    |                            |
| Tracks:                    |    | Tracks:                    |    | Inherits:                  |
| - Pages visited (slugs)   |    | - Conversation messages    |    | - All visitor_id data      |
| - Bills engaged with      |    | - Page context per message |    | - All linked session data  |
| - Session count            |    | - Streaming state          |    | + Jurisdiction/district    |
| - First seen / last seen   |    |                            |    | + Notification preferences |
| - Device/browser fingerprint|   |                            |    | + Opinion vectors (Polis)  |
| - Jurisdiction hints       |    |                            |    | + Verified identity (XID)  |
+----------------------------+    +----------------------------+    +----------------------------+
        |                                  |                                  |
        +------ linked by visitor_id ------+------ merged on auth login ------+
```

### Why Three Layers?

- **Visitor** solves the "same person, different tab" and "return visitor" problems. It's the durable anonymous identity.
- **Session** stays as-is (tab-scoped conversation state). No changes needed to the existing sessionStorage model.
- **Member** is the Memberstack-authenticated identity. It's the goal state, but it may take months before meaningful login adoption.

The key insight: **we start collecting visitor-level data immediately**, so when a user eventually logs in, their profile isn't empty — it already has engagement history, topic interests, and behavioral signals.

---

## Phase 0: Fix the IP Blind Spot (Immediate)

**Problem**: Every query logs `client_ip: 127.0.0.1` because the real client IP is lost in the nginx -> DDP-API -> VoteBot proxy chain.

**Fix**: Propagate `X-Forwarded-For` through DDP-API and extract it in VoteBot.

### Changes Required

**1. DDP-API** (`~/DDP-API/app/routes/votebot.py`):
- Pass `X-Forwarded-For` and `X-Real-IP` headers from the incoming request through to VoteBot
- DDP-API already receives real IPs from nginx (which sets `X-Forwarded-For`)

**2. VoteBot WebSocket** (`src/votebot/api/routes/websocket.py`):
- Already extracts `x-forwarded-for` (line 362): `websocket.headers.get("x-forwarded-for", "").split(",")[0].strip()`
- But DDP-API doesn't forward it, so it's always empty and falls back to `websocket.client.host` = `127.0.0.1`

**3. VoteBot REST** (`src/votebot/api/routes/chat.py`):
- Same pattern: extract from `X-Forwarded-For` header

**Effort**: ~30 minutes. One line in DDP-API proxy, verify in VoteBot.

**Impact**: Immediately enables user segmentation by IP in production logs. We'd finally know if 278 queries = 5 users or 50 users.

---

## Phase 1: Anonymous Visitor Tracking (Widget + Redis)

### 1.1 Widget: Persistent Visitor ID

Add a `visitor_id` to the chat widget, stored in `localStorage` (not `sessionStorage`), so it persists across tabs and sessions.

**`chat-widget/src/websocket.js` changes:**

```javascript
// New: localStorage-based visitor identity (persists across sessions/tabs)
const VISITOR_KEY = 'ddp_votebot_visitor_id';

function _getOrCreateVisitorId() {
    try {
        var existing = localStorage.getItem(VISITOR_KEY);
        if (existing) return existing;
        // Generate a durable anonymous ID
        var id = 'v_' + crypto.randomUUID();
        localStorage.setItem(VISITOR_KEY, id);
        return id;
    } catch (e) {
        // localStorage blocked (incognito, etc.) — fall back to session-scoped
        return 'v_temp_' + Math.random().toString(36).substr(2, 12);
    }
}
```

**Send visitor_id with every WebSocket connection and message:**

```javascript
// In connect(): append visitor_id to URL
var visitorId = _getOrCreateVisitorId();
var url = wsUrl + '?session_id=' + (sessionId || '') + '&visitor_id=' + visitorId;

// In send(): include visitor_id in payload
function send(data) {
    data.visitor_id = _getOrCreateVisitorId();
    ws.send(JSON.stringify(data));
}
```

### 1.2 Server: Visitor Profile in Redis

**New Redis key namespace:** `votebot:visitor:{visitor_id}`

```python
# In services/redis_store.py — new visitor profile methods

VISITOR_KEY = "votebot:visitor:{visitor_id}"
VISITOR_TTL = 90 * 24 * 3600  # 90 days

async def get_visitor_profile(self, visitor_id: str) -> dict | None:
    """Get or initialize a visitor profile."""

async def update_visitor_activity(self, visitor_id: str, page_context: dict, session_id: str):
    """Record a visitor interaction — page view, session link, timestamp."""

async def link_visitor_to_member(self, visitor_id: str, member_id: str):
    """Associate anonymous visitor with authenticated Memberstack member."""

async def get_visitor_for_member(self, member_id: str) -> str | None:
    """Reverse lookup: member_id -> visitor_id."""
```

**Visitor profile schema (Redis JSON):**

```json
{
  "visitor_id": "v_abc123...",
  "created_at": "2026-03-06T...",
  "last_seen": "2026-03-06T...",
  "session_count": 14,
  "total_queries": 87,
  "sessions": ["sess_1", "sess_2", "..."],
  "pages_visited": [
    {"slug": "stop-insider-trading-act-hr7008-2026", "type": "bill", "count": 5, "last_visit": "..."},
    {"slug": "election-amendments-sb153-2026", "type": "bill", "count": 3, "last_visit": "..."}
  ],
  "jurisdictions_engaged": ["US", "FL", "UT"],
  "device_fingerprints": ["Mac-Chrome-1920x1080", "iPhone-Safari-390x844"],
  "member_id": null,
  "ip_addresses": ["73.12.xxx.xxx"]
}
```

**TTL**: 90 days from last activity. Refreshed on every interaction. This keeps profiles alive for active visitors and naturally expires inactive ones.

### 1.3 WebSocket Endpoint Changes

**`src/votebot/api/routes/websocket.py`:**

```python
@router.websocket("/ws/chat")
async def websocket_chat_endpoint(
    websocket: WebSocket,
    session_id: str = Query(default=None),
    visitor_id: str = Query(default=None),  # NEW
):
    # ... existing session setup ...

    # Track visitor
    if visitor_id:
        redis_store = get_redis_store()
        await redis_store.update_visitor_activity(
            visitor_id=visitor_id,
            page_context=page_context_data,
            session_id=session_id,
        )

    # Include visitor_id in session state
    if session_id in sessions:
        sessions[session_id]["visitor_id"] = visitor_id
```

### 1.4 Query Logger Enhancement

Add `visitor_id` to every JSONL log entry:

```json
{
  "timestamp": "...",
  "session_id": "abc123",
  "visitor_id": "v_def456",
  "client_ip": "73.12.xxx.xxx",
  "user_agent": "...",
  "message": "...",
  "..."
}
```

This enables production evaluation to segment by visitor (not just session), answering: "How many distinct people use VoteBot? What's the per-visitor query distribution?"

### 1.5 Behavioral Signal Collection

On each message, update the visitor profile with lightweight signals:

```python
# In handle_user_message(), after processing:
if visitor_id:
    await redis_store.update_visitor_activity(
        visitor_id=visitor_id,
        page_context=page_context_data,
        session_id=session_id,
    )
    # Increment query count, update last_seen, add page to pages_visited
```

**What we DON'T store in the visitor profile:**
- Raw message text (privacy risk; stays in query logs only)
- Full conversation history (stays in session memory)
- Personally identifiable information (no name, email, etc. until Memberstack login)

**What we DO store:**
- Pages visited (slugs + counts) — interest signals for personalization
- Jurisdictions engaged — for geographic inference
- Session count + query count — engagement depth
- Device fingerprints (UA-derived) — for cross-device recognition hints
- Timestamps — activity patterns

---

## Phase 2: Memberstack Authentication Integration

### 2.1 How Memberstack Works with Webflow

Memberstack provides:
- Login/signup modals on the DDP Webflow site (native integration)
- A JavaScript SDK (`window.$memberstackDom`) available on all pages
- JWT tokens for authenticated API calls
- Member metadata fields (custom JSON per member)
- Webhooks for signup, login, profile update events

### 2.2 Widget: Detect Memberstack Login

When the chat widget loads, check if the user is logged into Memberstack:

```javascript
// In widget.js initWidgetWithContext():
function detectMemberstackAuth() {
    // Memberstack v2 SDK
    if (window.$memberstackDom) {
        return window.$memberstackDom.getCurrentMember().then(function(result) {
            if (result && result.data) {
                return {
                    member_id: result.data.id,
                    email: result.data.auth.email,
                    // Don't send full profile — just the ID for server-side lookup
                };
            }
            return null;
        }).catch(function() { return null; });
    }
    return Promise.resolve(null);
}
```

**Pass member info in WebSocket connection:**

```javascript
var memberInfo = await detectMemberstackAuth();
var url = wsUrl
    + '?session_id=' + (sessionId || '')
    + '&visitor_id=' + visitorId
    + (memberInfo ? '&member_id=' + memberInfo.member_id : '');
```

### 2.3 Server: Member-Visitor Linking

When a `member_id` arrives for the first time alongside a `visitor_id`:

```python
# In websocket endpoint:
if visitor_id and member_id:
    await redis_store.link_visitor_to_member(visitor_id, member_id)
    # All historical visitor data (pages, jurisdictions, session count)
    # is now associated with the authenticated member
```

**The merge is idempotent**: calling `link_visitor_to_member` again with the same pair is a no-op. If a member logs in from a different device (new visitor_id), both visitor profiles link to the same member.

### 2.4 Server: Memberstack Token Validation (Optional)

For sensitive operations (submitting Polis opinions, changing preferences), validate the Memberstack JWT server-side:

```python
# New: services/memberstack.py
import httpx

async def validate_memberstack_token(token: str) -> dict | None:
    """Validate a Memberstack JWT and return member data."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://admin.memberstack.com/members/current",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 200:
            return resp.json()
    return None
```

This is only needed for write operations. Read-only chat doesn't require auth validation — the visitor_id is sufficient for personalization.

### 2.5 Member Profile (Redis + Memberstack)

Authenticated members get an enriched profile:

```json
{
  "member_id": "mem_abc123",
  "visitor_ids": ["v_def456", "v_ghi789"],
  "jurisdiction": "FL",
  "district": "12",
  "notification_preferences": { "email_digest": true, "bill_alerts": ["HB 47"] },
  "opinion_vectors": {},
  "polis_xid": "votebot_mem_abc123",
  "created_at": "...",
  "last_login": "..."
}
```

**Storage**: Redis (`votebot:member:{member_id}`, no TTL — permanent for authenticated users) + Memberstack metadata API for durable storage.

---

## Phase 3: Personalized VoteBot Responses

With visitor/member identity, VoteBot can personalize:

### 3.1 Context-Aware Welcome Messages

```
# Returning anonymous visitor who previously viewed FL bills:
"Welcome back! I see you've been following Florida legislation.
 Ask me anything about the bills you've been reading."

# Authenticated member with known jurisdiction:
"Hi [name]! I have updates on 3 bills in Florida District 12
 since your last visit. Want to hear about them?"

# New visitor, first time:
"Welcome to VoteBot! I can help you understand legislation
 across 8 states. What would you like to know?"
```

### 3.2 Jurisdiction Inference

For anonymous visitors, infer likely jurisdiction from:
1. **Pages visited** — if 80% of bill views are FL bills, suggest FL context
2. **IP geolocation** — coarse state-level inference from client IP (now that we'll have real IPs)
3. **Explicit selection** — "Which state are you interested in?" (asked once, stored in visitor profile)

### 3.3 Continuity Across Sessions

```
User returns to a bill page they visited 3 days ago:

VoteBot: "Welcome back to HB 47. Since your last visit,
 this bill passed the Senate Judiciary Committee on March 4th.
 The vote was 8-2. Want to know who voted and why?"
```

This requires:
- Visitor profile knows they viewed this slug before (Phase 1)
- Bill version sync has `last_status` data (already exists)
- Diff between "last visit" and "current status" (new logic in agent.py)

### 3.4 Connection to Polis/Jigsaw Opinion Vectors

From `PLAN-votebot-polis-jigsaw.md`, the opinion elicitation system needs:

1. **Persistent identity** across sessions — to build opinion vectors incrementally
2. **Consent tracking** — which users have confirmed their opinions for Polis submission
3. **Polis XID** — maps VoteBot identity to Polis participant

The three-layer model solves all three:
- **Anonymous visitors** get passive opinion extraction stored against `visitor_id` (Phase 2 of Polis plan)
- **Authenticated members** get consent flow + Polis vote submission with `polis_xid = "votebot_{member_id}"`
- **Merge on login** — anonymous opinion vectors transfer to the member profile

---

## Phase 4: Analytics and Evaluation Improvements

### 4.1 Enhanced Production Evaluator

Update `scripts/evaluate_production.py` to:
- Segment by `visitor_id` instead of just `session_id`
- Report unique visitors per day/week
- Identify power users vs. casual visitors
- Track return visitor rate
- Measure queries-per-visitor distribution

### 4.2 Visitor Funnel Metrics

New metrics enabled by visitor tracking:
- **Unique visitors per day** (deduped by visitor_id)
- **Return visitor rate** (visitors who come back within 7 days)
- **Engagement depth** (avg queries per visitor per session)
- **Bill engagement breadth** (avg distinct bills per visitor)
- **Conversion rate** (anonymous -> Memberstack signup)
- **Jurisdiction distribution** (inferred from behavior, not just page_context)

---

## Implementation Roadmap

### Phase 0: X-Forwarded-For Fix (Day 1)
- [ ] Update DDP-API proxy to forward `X-Forwarded-For` and `X-Real-IP` headers
- [ ] Verify VoteBot extracts real client IP from forwarded headers
- [ ] Deploy and confirm IPs appear in query logs

### Phase 1: Anonymous Visitor Tracking (Week 1-2)
- [ ] Add `visitor_id` to chat widget (`localStorage`-based)
- [ ] Pass `visitor_id` in WebSocket connection URL
- [ ] Add visitor profile CRUD to `redis_store.py`
- [ ] Update WebSocket handler to track visitor activity
- [ ] Add `visitor_id` to query logger JSONL output
- [ ] Update `evaluate_production.py` to segment by visitor
- [ ] Rebuild chat widget (`npm run build`)
- [ ] Deploy widget + backend, verify visitor tracking in logs

### Phase 2: Memberstack Integration (Week 3-4)
- [ ] Add Memberstack SDK detection to chat widget
- [ ] Pass `member_id` in WebSocket connection when authenticated
- [ ] Implement visitor-to-member linking in Redis
- [ ] Add `memberstack_api_key` to config/settings
- [ ] Optional: server-side Memberstack JWT validation for write operations
- [ ] Add member profile schema to Redis
- [ ] Deploy and test with Memberstack test account

### Phase 3: Personalization (Week 5-7)
- [ ] Context-aware welcome messages based on visitor history
- [ ] Jurisdiction inference from visitor behavior + IP geolocation
- [ ] "Since your last visit" bill status diffs
- [ ] Return visitor continuity in agent.py system prompt
- [ ] A/B test: personalized vs. generic welcome messages

### Phase 4: Analytics + Polis Bridge (Week 8+)
- [ ] Enhanced production evaluator with visitor segmentation
- [ ] Visitor funnel dashboard (Grafana or simple script)
- [ ] Wire visitor/member identity into Polis XID generation
- [ ] Consent flow for opinion vector submission (from Polis plan Phase 6)

---

## Data Privacy Considerations

### What We Store for Anonymous Visitors
- **Visitor ID**: Opaque random UUID — no PII
- **Pages visited**: Bill/legislator/org slugs with counts — public content identifiers, not PII
- **Jurisdictions engaged**: State codes inferred from page views — no geolocation data stored
- **Session counts and timestamps**: Activity metadata — no PII
- **Device fingerprints**: User-agent derived categories ("Mac-Chrome") — no unique fingerprinting
- **IP addresses**: Stored in query logs (already happening); optionally in visitor profile for geo inference

### What We Don't Store
- **Raw messages**: Not in visitor profiles (only in query logs with existing retention)
- **Names, emails**: Not until Memberstack login (and then it's user-provided)
- **Cross-site tracking**: No third-party cookies, no analytics pixels, no ad tech

### User Control
- `localStorage.removeItem('ddp_votebot_visitor_id')` resets anonymous identity
- Memberstack account deletion cascades to visitor profile cleanup via webhook
- No data is shared with third parties (except Polis, with explicit consent per the Polis plan)

### GDPR/CCPA Notes
- Anonymous visitor profiles don't contain PII under most interpretations (opaque UUIDs + public content slugs)
- Authenticated member profiles inherit Memberstack's privacy compliance (they handle consent, data export, deletion)
- Consider a "clear my data" option in the widget for jurisdictions that require it
- Query logs with IP addresses may be PII — consider hashing IPs after 30 days if retention concerns arise

---

## Technical Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| `localStorage` blocked (incognito/Safari ITP) | Visitor ID is per-session instead of persistent | Fall back to `sessionStorage` with `v_temp_` prefix; still useful for single-session tracking |
| Redis memory growth from visitor profiles | 90-day profiles for thousands of visitors | Each profile is ~1-2KB; 10K visitors = ~20MB. Set TTL aggressively; prune `pages_visited` to top 50 |
| Memberstack SDK not loaded when widget initializes | Can't detect authenticated state | Retry detection with 2s delay; or listen for `memberstack:ready` custom event |
| Multiple visitor_ids per person (different browsers/devices) | Fragmented anonymous profiles | On Memberstack login, link all visitor_ids to member; pre-login, this is an accepted limitation |
| DDP-API doesn't forward headers correctly | IP still 127.0.0.1 | Test with `curl -H "X-Forwarded-For: 1.2.3.4"` through the full proxy chain |
| Privacy backlash from tracking | User trust erosion | Keep tracking minimal (slugs, not messages); be transparent in privacy policy; provide opt-out |

---

## Open Questions for Discussion

1. **Should we store visitor profiles in Redis or a durable database?** Redis with 90-day TTL is simple but lossy. DynamoDB or PostgreSQL would be permanent but adds infrastructure. Recommendation: Start with Redis; move to DynamoDB only if we need profiles beyond 90 days (i.e., when Polis opinion vectors need long-term storage).

2. **When should we prompt for Memberstack signup?** Options: never (let the website handle it), after N sessions, after expressing opinions (Polis consent flow triggers it), or on "save my preferences" action. Recommendation: Don't prompt from the widget. Let Memberstack signup happen on the DDP website; the widget just detects it.

3. **Should the widget show different UI for authenticated users?** E.g., "Logged in as [name]" badge, or a "my profile" button. Recommendation: Not initially. The chat experience should be identical; personalization is invisible (better welcome messages, jurisdiction inference).

4. **How do we handle the Webflow -> Memberstack -> VoteBot auth flow?** Webflow pages embed the widget. Memberstack JS SDK runs on Webflow. The widget reads Memberstack state. VoteBot validates server-side. This is a four-system chain. Recommendation: Widget-side detection only (Phase 2.2); defer server-side validation until we need write operations.

5. **IP geolocation service?** For jurisdiction inference from IP. Options: MaxMind GeoLite2 (free, self-hosted DB), ip-api.com (free tier), or just use the IP as a session-level hint without geocoding. Recommendation: Defer — behavioral jurisdiction inference (which state's bills they read) is more accurate and doesn't require an external service.
