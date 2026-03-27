# Jigsaw Stage C: Identity, Consent, and Voter Verification

**Parent plan:** [PLAN-jigsaw-overview.md](PLAN-jigsaw-overview.md)
**Status:** Blocked on Stage B validation gates.

---

## Goal

Authenticated identity, durable opinion storage, and voter verification. This is the bridge to civic legitimacy — the stage where anonymous chat opinions become attributable to real people in real districts. Users who choose to verify gain full weight in opinion aggregation; those who don't still participate at reduced weight.

---

## Prerequisites

Stage B must demonstrate that users engage with opinion features before investing in identity infrastructure:

- [ ] **Prompt acceptance rate** — >30% of users interact with contextual position prompts
- [ ] **Position depth** — >20% of prompted users reach 7+ confirmed positions
- [ ] Both gates sustained for 2+ weeks on production traffic

Until these gates pass, Stage C work is limited to Catalist procurement and Memberstack API exploration.

---

## 1. Consent Flow (In-Chat, Conversational)

The consent flow is entirely conversational — no modals, no forms, no redirects.

### Trigger Conditions

- [ ] 3+ opinion signals detected for the current visitor in the active conversation
- [ ] Natural conversation pause (user has received a substantive answer, no pending question)
- [ ] Visitor has not previously declined consent in this session

### Flow

```
VoteBot: Based on our conversation, it sounds like you:
  • Support the proposed state funding formula (HB 123)
  • Oppose tying funding to local property tax
  • Want a faster implementation timeline

Would you like me to save these views? Your responses would be
anonymous and help show legislators what voters in your district think.

User: [confirms / edits / declines]
```

### User Actions

| Action | Behavior |
|---|---|
| **Confirm all** | Stances saved as-is, proceed to onboarding |
| **Edit stances** | User corrects in natural language, VoteBot re-summarizes, re-confirms |
| **Decline entirely** | Permanent for this session — no re-prompt. User can consent in a future session. |

### Implementation

- [ ] Build consent prompt generator that summarizes extracted stances in plain language
- [ ] Parse user confirmation/edit/decline from natural language response
- [ ] Log `consent_prompted`, `consent_confirmed`, `consent_edited`, `consent_declined` analytics events
- [ ] Store consent status on the opinion vector record
- [ ] Respect session-level decline flag — check before any consent prompt

---

## 2. Conversational Onboarding (In-Chat, Server-Side)

No external forms. No redirects. The entire account creation happens within the chat window.

### Step A: Account Creation (Low-Friction)

- [ ] After consent confirmed, VoteBot asks for name and email:
  ```
  VoteBot: To save your views, I just need your name and email.

  User: John Smith, john@example.com

  VoteBot: Got it — John Smith, john@example.com — is that right?

  User: Yes
  ```
- [ ] Parse name + email from free-text input using LLM tool (`create_voter_account`)
- [ ] Echo parsed data back for explicit user confirmation
- [ ] On confirmation, server creates Memberstack account via Admin API
- [ ] Password setup email sent automatically by Memberstack
- [ ] Opinions promoted from `visitor_id` Redis store (90-day TTL) to `member_id` PostgreSQL (permanent)
- [ ] Visitor-to-member mapping stored in Redis (see Redis keys below)

### Step B: Voter Verification (Optional)

- [ ] VoteBot asks for date of birth and zip code:
  ```
  VoteBot: Want to verify your voter registration? This lets us attribute
  your views to your specific district. I'd just need your date of birth
  and zip code.

  User: 03/15/1990, 80202

  VoteBot: March 15, 1990 and zip code 80202 — correct?

  User: Yep
  ```
- [ ] Parse DOB + zip from free-text input
- [ ] Echo parsed data back for confirmation
- [ ] On confirmation, server calls Catalist Fusion Light API for voter verification
- [ ] On match: store DWID + districts on Memberstack profile metadata and PostgreSQL `verified_voters` table
- [ ] On no match: graceful failure with retry option
  ```
  VoteBot: I wasn't able to find a matching voter registration. This can
  happen if your registration uses a different name or address. You can
  try again with different info, or continue without verification — your
  views will still be saved.
  ```

### Decline Handling

| Scenario | Result |
|---|---|
| User declines Step B | Active member at 0.9x weight, no district attribution |
| Catalist returns no match | Unverified member at 0.9x weight, retry available |
| User declines Step A | Opinions remain anonymous at visitor-level weight |

---

## 3. Catalist Fusion Light API Integration

### Authentication

- [ ] OAuth2 client credentials flow against `auth.catalist.us`
- [ ] Retrieve 24-hour bearer token
- [ ] **Token MUST be cached and reused** — do not request a new token per call
- [ ] Token refresh on 401 response or proactive refresh at 23-hour mark

### Verification Call

- [ ] POST to workflow endpoint (sync execution, single-record)
- [ ] Input fields:

| Field | Source | Required |
|---|---|---|
| `firstName` | Parsed from chat | Yes |
| `lastName` | Parsed from chat | Yes |
| `dob` | Parsed from chat | Yes |
| `zip` | Parsed from chat | Yes |
| `address` | — | No (not required for zip-based matching) |

- [ ] Output fields:

| Field | Description |
|---|---|
| `dwid` | Nationally unique person ID (Catalist Data Warehouse ID) |
| `congressional_district` | e.g., `CO-01` |
| `state_senate_district` | e.g., `CO-SD-31` |
| `state_house_district` | e.g., `CO-HD-06` |
| `registration_status` | Active, inactive, cancelled, etc. |

### Infrastructure

- [ ] EC2 Elastic IP for Catalist IP allowlisting
- [ ] Credentials stored in environment variables (not config files)

---

## 4. Services

### `services/catalist.py`

```python
class CatalistService:
    """Catalist Fusion Light API client with token management."""

    def __init__(self, client_id: str, client_secret: str,
                 workflow_id: str, audience: str):
        self._token: str | None = None
        self._token_expires_at: float = 0
        # ...

    async def _get_token(self) -> str:
        """Get cached bearer token, refreshing if expired."""
        if self._token and time.time() < self._token_expires_at - 3600:
            return self._token
        # OAuth2 client credentials → auth.catalist.us
        # Cache token, set expiry
        ...

    async def verify_voter(
        self, first_name: str, last_name: str,
        dob: str, zip_code: str
    ) -> CatalistResult | None:
        """Verify voter and return DWID + districts, or None on no match."""
        token = await self._get_token()
        # POST to workflow endpoint
        # Parse response → CatalistResult(dwid, districts, status)
        ...
```

### `services/memberstack.py`

```python
class MemberstackService:
    """Memberstack Admin API client."""

    def __init__(self, admin_api_key: str):
        ...

    async def create_member(
        self, email: str, first_name: str, last_name: str
    ) -> MemberstackMember:
        """Create member account. Triggers password setup email."""
        ...

    async def update_member_metadata(
        self, member_id: str, metadata: dict
    ) -> None:
        """Store DWID, districts, verified status on member profile."""
        ...

    async def validate_memberstack_token(self, token: str) -> str | None:
        """Validate JWT token, return member_id or None."""
        ...

    async def get_member_by_email(self, email: str) -> MemberstackMember | None:
        """Look up existing member by email."""
        ...
```

### LLM Tool: `create_voter_account`

- [ ] Registered as an LLM tool for structured data extraction from chat
- [ ] Extracts: `first_name`, `last_name`, `email`, `dob`, `zip_code`
- [ ] Returns extracted fields for server-side processing (no direct API calls from LLM)

---

## 5. Returning Members

### Widget-Side Detection

- [ ] Widget calls `$memberstackDom.getCurrentMember()` on load
- [ ] If member is authenticated, retrieve JWT token
- [ ] Send JWT token over WebSocket during connection handshake

### Server-Side Validation

- [ ] Server validates JWT against Memberstack API via `validate_memberstack_token()`
- [ ] **Never trust bare `member_id` from client** — always validate the token
- [ ] On valid token: load member profile, restore opinion history, set participation tier
- [ ] On invalid/expired token: treat as anonymous visitor, do not error

---

## 6. Participation Tiers

Tiers are now **active** in this stage (passive in Stages A-B):

| Tier | Weight | Scope | Trigger |
|---|---|---|---|
| **Passive** | 0.5x | Aggregate only | Silent extraction, no consent |
| **Active anonymous** | 0.7x | Aggregate + session persistence | Consent confirmed, no account |
| **Active member** | 0.9x | Cross-device, durable | Memberstack account created |
| **Verified voter** | 1.0x | District attribution | Catalist DWID match |
| **Polis direct** | 1.0x | Full Polis participant | Voted directly in Polis embed |

- [ ] Implement weight multiplier in opinion aggregation queries
- [ ] Display tier-appropriate messaging in chat ("your views are saved", "verified for [district]")
- [ ] Log participation tier on all analytics events

---

## 7. PostgreSQL Tables Introduced

### `opinion_vectors`

```sql
CREATE TABLE opinion_vectors (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    visitor_id      VARCHAR(64),
    member_id       VARCHAR(64),
    bill_webflow_id VARCHAR(128) NOT NULL,
    landscape_version INTEGER NOT NULL,
    stances         JSONB NOT NULL,
    positions_covered INTEGER NOT NULL DEFAULT 0,
    consent_status  VARCHAR(20) NOT NULL DEFAULT 'none',
    polis_submitted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_visitor_bill_landscape
        UNIQUE (visitor_id, bill_webflow_id, landscape_version),
    CONSTRAINT uq_member_bill_landscape
        UNIQUE (member_id, bill_webflow_id, landscape_version)
);

CREATE INDEX idx_opinion_vectors_bill ON opinion_vectors (bill_webflow_id);
CREATE INDEX idx_opinion_vectors_member ON opinion_vectors (member_id);
CREATE INDEX idx_opinion_vectors_consent ON opinion_vectors (consent_status);
CREATE INDEX idx_opinion_vectors_updated ON opinion_vectors (updated_at);
```

### `verified_voters`

```sql
CREATE TABLE verified_voters (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    catalist_dwid           VARCHAR(64) UNIQUE NOT NULL,
    member_id               VARCHAR(64) UNIQUE NOT NULL,
    jurisdiction            VARCHAR(10),
    congressional_district  VARCHAR(20),
    state_senate_district   VARCHAR(20),
    state_house_district    VARCHAR(20),
    registration_status     VARCHAR(30),
    verified_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_verified_voters_member ON verified_voters (member_id);
CREATE INDEX idx_verified_voters_district ON verified_voters (congressional_district);
```

---

## 8. Redis Keys Introduced

| Key | Value | TTL |
|---|---|---|
| `votebot:visitor_to_member:{visitor_id}` | `member_id` string | Permanent |
| `votebot:member_to_visitors:{member_id}` | JSON array of `visitor_id` strings | Permanent |
| `votebot:member:{member_id}` | Member profile JSON (name, email, tier, districts) | Permanent |

These keys enable fast lookups for returning members and visitor-to-member migration without hitting PostgreSQL on every request.

---

## 9. Config Settings

| Setting | Source | Purpose |
|---|---|---|
| `memberstack_admin_api_key` | Environment variable | Memberstack Admin API authentication |
| `catalist_client_id` | Environment variable | Catalist OAuth2 client ID |
| `catalist_client_secret` | Environment variable | Catalist OAuth2 client secret |
| `catalist_workflow_id` | Environment variable | Catalist Fusion Light workflow identifier |
| `catalist_audience` | Environment variable | Catalist OAuth2 audience parameter |

- [ ] Add all settings to config schema with validation
- [ ] Document required env vars in deployment runbook
- [ ] Add health check endpoint that verifies Catalist + Memberstack connectivity

---

## 10. Privacy

- [ ] **PII in transit only** — name, DOB, and address are sent to Catalist for matching but never stored in VoteBot systems
- [ ] **Minimal retention** — only DWID and district codes are retained from Catalist response
- [ ] **Message redaction** — raw chat messages and responses subject to 90-day retention, then redacted (opinion vectors and aggregates preserved)
- [ ] **Consent audit trail** — all consent events logged with timestamps for compliance
- [ ] **No PII in analytics** — analytics events contain visitor_id/member_id references only, never name/email/DOB

---

## 11. Catalist Is NOT on Critical Path for PMF

Stages A and B validate product-market fit without any Catalist dependency. The core loop — users chat about bills, express opinions, see their views reflected — works entirely with anonymous visitors.

Catalist is on the critical path for:
- **Civic legitimacy** — "89 verified voters in CO-01 support this provision"
- **Anti-gaming** — one-person-one-vote via DWID deduplication
- **District reporting** — attributing opinions to specific legislative districts

Start Catalist procurement in parallel with Stages A-B, but do not block product development on it.

---

## 12. Cost Control Architecture

> **Why Stage C:** Identity tiers (anonymous → member → verified) enable tiered budgets. However, basic cost controls (per-session limits, rate limiting) should be introduced in Stage A when extraction adds LLM costs. This section defines the full architecture; Stage A implements layers 1, 3, and 4. Stage C adds layer 2 (per-user budgets) and layer 6 (identity incentives).

### Layer 1: Per-Session Budget (Stage A+)

Prevents a single conversation from burning excessive tokens.

| Tier | Max Turns | Max Tokens/Session |
|---|---|---|
| Anonymous | 8-12 | ~8k-12k |
| Member | 25-40 | ~25k-40k |
| Verified | 50+ | ~50k+ |

**UX at limit (value framing, not restriction):**
- At ~80%: "We've covered a lot here — want me to save your progress so you can pick this up later?"
- At 100%: "I can pause here for now. Your views are saved and you can continue anytime."

**Implementation:** Track in Redis `votebot:session_budget:{session_id}` — `{messages, tokens_used}`.

### Layer 2: Per-User Daily Budget (Stage C)

Prevents repeat usage from draining cost. Requires identity — anonymous users are tracked by `visitor_id`, members by `member_id`.

| Tier | Daily Token Budget |
|---|---|
| Anonymous | 15k-25k |
| Member | 50k-75k |
| Verified | 100k+ |

**UX:** Soft warning at ~80% → hard stop at 100% with "continue tomorrow" framing.

**Implementation:** Redis `votebot:daily_budget:{composite_id}` — `{tokens_used, window_start}`. Uses a **rolling 24-hour window** per user (not UTC midnight reset — simultaneous reset creates traffic/cost spikes). `composite_id` = `hash(visitor_id + IP + user_agent)` for anonymous users, `member_id` for authenticated members.

**Per-IP daily cap (anonymous abuse resistance):** Max 200k tokens/day per IP, regardless of visitor_id. Redis `votebot:ip_budget:{ip}`. This catches multi-session, multi-identity abuse from a single source.

### Layer 3: Rate Limiting + Abuse Detection (Stage A+)

Prevents bots, scripts, and edge-case abuse.

**Rate limits per IP / visitor_id:**
- Max 5 requests / 10 seconds
- Max 20 requests / minute

**Behavioral flags:**
- < 1 second between messages repeatedly
- Identical prompts repeated
- High token usage with low engagement (many tokens, few opinion signals)

**Progressive response:** slow responses → temporary throttle → block session.

**Implementation:** Redis `votebot:rate:{visitor_id}`, `votebot:ip_rate:{ip}`.

### Layer 4: Model Routing (Stage A+, biggest cost lever)

Use cheaper models for background tasks, reserve expensive models for user-facing responses.

| Task | Model Tier | Rationale |
|---|---|---|
| Intent detection | Cheap (Haiku/4o-mini) | Classification doesn't need reasoning |
| Opinion extraction | Cheap (Haiku/4o-mini) | Structured output from template prompt |
| Entity parsing (name/email/DOB) | Cheap (Haiku/4o-mini) | Simple field extraction |
| Landscape generation | Mid (Sonnet/4o) | Needs understanding of legislation |
| User-facing response | High (Opus/GPT-4.1) | Quality matters for UX |
| Summarization | Cheap (Haiku/4o-mini) | Condensation, not reasoning |

**Impact:** Can reduce total LLM cost by 50-80%. Opinion extraction (running on every bill-page message) is the highest-volume call and benefits most from cheap models.

### Layer 5: Token Budget Awareness (Stage B+)

Before generating a response, check remaining budget and adapt:

```python
if remaining_session_budget < estimated_response_cost:
    # Downgrade response model (GPT-4.1 → Sonnet/4o)
    # Or shorten response (add "concise: true" to system prompt)
    # Or skip opinion extraction this turn
```

**Token estimation:** Use conservative estimates (1.5-2x expected tokens) to avoid exceeding limits. Underestimate budget remaining, not overestimate.

**Response model fallback ladder:**
- Budget > 50%: full model (GPT-4.1/Opus)
- Budget 30-50%: mid model (Sonnet/4o)
- Budget < 30%: cheap model (Haiku/4o-mini) + concise system prompt

### Layer 5b: Budget Persistence (Stage A+)

Redis budget counters are volatile — eviction, restart, or TTL expiry can silently reset them. Persist to PostgreSQL for audit:

- [ ] Every 15 minutes, write current budget state (per composite_id and per IP) to a `budget_snapshots` table
- [ ] On Redis restart, restore from latest snapshot
- [ ] Audit trail enables cost analysis and abuse investigation after the fact

### Layer 6: Progressive Identity Incentive (Stage C)

Cost limits become a natural onboarding ramp — identity is a benefit unlock, not a restriction.

```
Anonymous user hits session limit:
  "I can keep going — want me to save your progress so you don't lose this?"
  → Triggers conversational onboarding Step A (name + email)

Member hits daily limit:
  "You've used today's quota. Verified voters get higher limits —
   want to verify your registration? It takes 30 seconds."
  → Triggers Step B (DOB + zip → Catalist)
```

This converts cost control into a conversion funnel. Users don't feel restricted; they feel like they're unlocking access.

### Layer 7: Cost Monitoring Dashboard (Non-negotiable)

Track daily:

| Metric | Why It Matters |
|---|---|
| Total tokens/day (by stage) | Are we within budget? |
| Cost per user (avg + p95) | Who's expensive? |
| **Cost per meaningful outcome** (cost per user with ≥3 positions) | Is the spend producing value? |
| **Cost per session cohort** (1-msg, 3-msg, 10-msg sessions) | Where does cost concentrate? |
| Token distribution (short/long/outlier sessions) | Are outliers driving spend? |
| Abuse signals (high-frequency, high-cost IPs/composites) | Are we being gamed? |
| Velocity alerts triggered | How often does the proactive guard fire? |

### Layer 8: Kill Switch (Non-negotiable)

Two-level automatic cost protection:

**Velocity guard (proactive):** Triggers on rate-of-spend, catches spikes before the daily threshold is reached.
```python
if spend_last_5min > COST_VELOCITY_THRESHOLD:
    # Throttle: reduce anonymous session limit, pause extraction
    # Alert admin via Slack immediately
```

**Daily threshold (reactive):** Triggers when total daily spend exceeds budget.
```python
if daily_spend > COST_ALERT_THRESHOLD:
    # Reduce anonymous session limit: 10 → 6 turns
    # Reduce token caps by 30%
    # Switch all extraction to cheapest model
    # Switch response model to mid-tier
    # Alert admin via Slack
```

**Config settings (new):**

| Setting | Default | Description |
|---|---|---|
| `SESSION_TOKEN_LIMIT_ANONYMOUS` | 10000 | Max tokens per anonymous session |
| `SESSION_TOKEN_LIMIT_MEMBER` | 30000 | Max tokens per member session |
| `SESSION_TOKEN_LIMIT_VERIFIED` | 50000 | Max tokens per verified session |
| `DAILY_TOKEN_LIMIT_ANONYMOUS` | 20000 | Daily token budget for anonymous visitors |
| `DAILY_TOKEN_LIMIT_MEMBER` | 60000 | Daily token budget for members |
| `DAILY_TOKEN_LIMIT_VERIFIED` | 100000 | Daily token budget for verified voters |
| `RATE_LIMIT_PER_10S` | 5 | Max requests per 10 seconds |
| `RATE_LIMIT_PER_MIN` | 20 | Max requests per minute |
| `COST_ALERT_THRESHOLD` | 50.00 | Daily USD spend that triggers kill switch |
| `COST_VELOCITY_THRESHOLD` | 10.00 | USD spend in 5-min window that triggers proactive throttle |
| `IP_DAILY_TOKEN_LIMIT` | 200000 | Max tokens per IP per day (anonymous abuse cap) |
| `RESPONSE_MODEL_MID` | sonnet | Mid-tier model for budget-constrained responses |
| `EXTRACTION_MODEL` | haiku | Model used for opinion extraction |
| `RESPONSE_MODEL` | gpt-4.1 | Model used for user-facing responses |

### Redis Keys Introduced (Cost Control)

| Key Pattern | Value | TTL |
|---|---|---|
| `votebot:session_budget:{session_id}` | `{messages, tokens_used}` | Session TTL (30 min) |
| `votebot:daily_budget:{composite_id}` | `{tokens_used, window_start}` | Rolling 24 hours |
| `votebot:ip_budget:{ip}` | `{tokens_used, window_start}` | Rolling 24 hours |
| `votebot:spend_velocity` | Rolling 5-min spend total | 5 minutes |
| `votebot:rate:{visitor_id}` | Request timestamps | 60 seconds |
| `votebot:ip_rate:{ip}` | Request timestamps | 60 seconds |
| `votebot:daily_spend` | Running USD total | 24 hours |

---

## 13. What This Stage Does NOT Include

- Polis integration or vote submission (Stage D)
- Clustering or PCA analysis (Stage D)
- Opinion maps or visualizations on the website (Stage D)
- Emergent position detection (Stage E)
- Any user-facing opinion reports

This stage is strictly about identity, consent, and verification infrastructure. Opinion data flows into PostgreSQL but is not yet surfaced anywhere beyond the chat conversation itself.

---

## Risk Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| Users won't create accounts | High | Consent flow is conversational and low-friction; account creation is 2 messages |
| Catalist API unavailable or slow | Medium | Graceful degradation — user continues as unverified member at 0.9x |
| Memberstack API rate limits | Low | Cache member profiles in Redis; batch operations where possible |
| PII exposure in logs | High | Strict log filtering; no PII in analytics events; message redaction at 90 days |
| Token theft via WebSocket | Medium | Server-side JWT validation on every request; never trust client-provided member_id |
