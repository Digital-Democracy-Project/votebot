# Jigsaw Stage A: Personalization + Silent Opinion Extraction

**Parent plan:** [PLAN-jigsaw-overview.md](PLAN-jigsaw-overview.md)
**Status:** Ready for implementation

---

## Goal

Deliver visible value to users through **personalized responses** while silently collecting opinion data to validate extraction accuracy. No user-facing opinion features yet — this stage proves we can extract opinions from natural conversation before building anything that depends on it.

---

## Prerequisites

The analytics system is already implemented and provides the foundation for this stage:

- **visitor_id** — stable cross-session visitor identity
- **conversation tracking** — session_id, conversation_id linking multi-turn exchanges
- **intent classification** — primary_intent field on every query_processed event

See [user-analytics-logging.md](user-analytics-logging.md) for the full analytics schema and implementation details.

---

## 1. Personalization (Immediate User Value)

### Visitor Profiles in Redis

- [ ] Create Redis visitor profile at `votebot:visitor:{visitor_id}`
- [ ] Store profile as JSON with fields:
  - `pages_visited` — list of page URLs with timestamps
  - `jurisdictions_engaged` — set of jurisdictions the visitor has interacted with
  - `session_count` — total sessions observed
  - `first_seen` — timestamp of first visit
  - `last_seen` — timestamp of most recent visit
  - `bills_viewed` — list of bill_webflow_ids with view counts
- [ ] Set 90-day TTL on all visitor profile keys
- [ ] Update profile on every chat interaction and page navigation event

### Context-Aware Welcome Messages

- [ ] Detect returning visitors via visitor profile lookup
- [ ] Vary welcome message based on visitor history:
  - New visitor: standard welcome
  - Returning visitor (same jurisdiction): "Welcome back — here's what's changed..."
  - Returning visitor (new jurisdiction): acknowledge the shift, offer orientation
- [ ] Surface relevant context without being intrusive

### Jurisdiction Inference

- [ ] Infer visitor's primary jurisdiction from behavioral signals:
  - Pages visited (bill jurisdiction)
  - Explicit jurisdiction mentions in chat
  - `jurisdictions_engaged` frequency in visitor profile
- [ ] Use inferred jurisdiction to prioritize search results and bill suggestions
- [ ] Allow inference to be overridden by explicit user statements

### "Since Your Last Visit" Bill Status Diffs

- [ ] For returning visitors, compute bill status changes since `last_seen`
- [ ] Generate a concise diff of bills the visitor previously viewed:
  - Status changes (e.g., "passed committee", "signed into law")
  - New actions or amendments
- [ ] Present diffs proactively in the welcome message for returning visitors
- [ ] Only surface diffs for bills the visitor has actually viewed (not all bills in jurisdiction)

---

## 2. Silent Opinion Extraction (Data Collection Only)

All opinion extraction in this stage is **feature-flagged and logging-only** — no user-facing elicitation, no prompting for opinions, no consent flows.

### Simplified Landscape Generation

- [ ] Build landscape generation pipeline seeded with known data:
  - **Org positions** from Webflow CMS (existing member organization stances)
  - **Bill summary** from bill enrichment data
  - **Canonical dimensions**: funding, timeline, scope, enforcement
- [ ] Constraint principle: **semi-structured, not fully generative**
  - Seed with known dimensions and org positions
  - LLM expands within constraints (e.g., infers sub-positions, fills gaps)
  - Do NOT use full open-ended LLM landscape generation yet
- [ ] Store landscapes in PostgreSQL `opinion_landscapes` table
- [ ] Cache landscapes in Redis at `votebot:landscape:{bill_webflow_id}`
- [ ] Track landscape versions via `votebot:landscape:version:{bill_webflow_id}`

### Basic Opinion Extraction

- [ ] On every bill-page message, run opinion extraction **async, post-response**
  - Do not add latency to the user-facing response
  - Trigger extraction when `query_processed` event has `primary_intent: bill`
- [ ] Extract opinion signals from user messages:
  - Match user language against landscape positions
  - Assign confidence score to each extracted stance
  - Capture evidence text (the user's words that indicate the stance)
- [ ] Write `OpinionSignal` records to PostgreSQL `opinion_signals` table
- [ ] Gate on confidence: only persist signals with confidence >= 0.7
- [ ] Feature flag: `JIGSAW_SILENT_EXTRACTION_ENABLED` (default: false)

### Extraction Audit Dashboard

- [ ] Build a review script or Jupyter notebook that:
  - Pulls extracted stances from `opinion_signals`
  - Displays them alongside the source user messages
  - Allows manual judgment (agree/disagree with extraction)
  - Computes agreement metrics (Cohen's kappa)
- [ ] Use this for the 50-message validation gate (see below)

### Analytics Integration

- [ ] Ensure `query_processed` events with `primary_intent: bill` trigger extraction
- [ ] Log extraction results as analytics events (signal count, confidence distribution)
- [ ] Track extraction latency separately from response latency

---

## 3. PostgreSQL Tables Introduced

### `opinion_landscapes`

| Column | Type | Description |
|---|---|---|
| `id` | SERIAL PRIMARY KEY | Auto-incrementing ID |
| `bill_webflow_id` | VARCHAR NOT NULL | Webflow CMS ID of the bill |
| `version` | INTEGER NOT NULL | Landscape version number |
| `landscape` | JSONB NOT NULL | Full landscape structure (dimensions, positions, org stances) |
| `total_positions` | INTEGER | Count of positions in this landscape |
| `created_at` | TIMESTAMP DEFAULT NOW() | Creation timestamp |

Unique constraint on `(bill_webflow_id, version)`.

### `opinion_signals`

| Column | Type | Description |
|---|---|---|
| `signal_id` | UUID PRIMARY KEY | Unique signal identifier |
| `visitor_id` | VARCHAR NOT NULL | Analytics visitor ID |
| `member_id` | VARCHAR NULL | Memberstack member ID (null in this stage) |
| `session_id` | VARCHAR NOT NULL | Analytics session ID |
| `conversation_id` | VARCHAR NOT NULL | Conversation ID linking multi-turn exchange |
| `bill_webflow_id` | VARCHAR NOT NULL | Webflow CMS ID of the bill discussed |
| `jurisdiction` | VARCHAR | State/jurisdiction code |
| `elicitation_mode` | VARCHAR NOT NULL | Always `'silent'` in this stage |
| `position_stances` | JSONB NOT NULL | Array of {position_id, stance, confidence} |
| `novel_claims` | JSONB | Claims not matching any known position |
| `user_message` | TEXT NOT NULL | The user's original message |
| `bot_response` | TEXT | VoteBot's response to the message |
| `created_at` | TIMESTAMP DEFAULT NOW() | Extraction timestamp |

Index on `(bill_webflow_id, created_at)` and `(visitor_id, created_at)`.

---

## 4. Redis Keys Introduced

| Key Pattern | Value | TTL | Description |
|---|---|---|---|
| `votebot:visitor:{visitor_id}` | JSON object | 90 days | Visitor profile (pages_visited, jurisdictions_engaged, session_count, first_seen, last_seen, bills_viewed) |
| `votebot:landscape:{bill_webflow_id}` | JSON object | None (invalidated on version change) | Cached landscape for a bill |
| `votebot:landscape:version:{bill_webflow_id}` | Integer | None | Current landscape version counter |

---

## 5. Validation Gates

These gates **must pass** before proceeding to Stage B. Do not skip them.

### Gate 1: Landscape Quality

- [ ] Generate simplified landscapes for **10 real bills**
- [ ] Human-evaluate each landscape on a 1-5 rubric (dimensions: completeness, accuracy, position clarity, org alignment)
- [ ] **Go/no-go: average score >= 3.5/5**

### Gate 2: Extraction Accuracy

- [ ] Run extraction on **50 real production messages** from bill pages
- [ ] Two human raters independently judge each extraction
- [ ] Compute inter-rater agreement using **Cohen's kappa**
- [ ] **Go/no-go: kappa >= 0.6**

### Gate 3: Opinion Prevalence

- [ ] Measure: what percentage of bill-page messages contain extractable opinion language?
- [ ] Report the distribution (informational, no hard gate — but informs Stage B design)

### Gate 4: Extraction Depth (Hard KPI)

- [ ] Measure: percentage of bill-page conversations with **>= 3 positions extracted**
- [ ] **Target: > 10% of bill-page conversations**

---

## 6. Success Metrics

| Metric | Baseline | Target | Measurement |
|---|---|---|---|
| Queries per session | Current avg | **+20% vs baseline** | Analytics: query count per session_id |
| Bill-page conversations with opinion language | Unknown | **> 15%** | Extraction pipeline: % of conversations with >= 1 signal |
| Conversations with 3+ positions extracted | Unknown | **> 10%** | Extraction pipeline: % of conversations with >= 3 stances |

---

## 7. Parallel Workstream: Catalist Procurement

This is the **longest lead-time item** in the Jigsaw roadmap and is NOT on the critical path for Stage A. Start it now so it is ready when needed in later stages.

- [ ] Initial outreach to Catalist
- [ ] Review data sharing agreement and terms
- [ ] Specify voter file fields and matching workflow needed for Stage C
- [ ] Identify technical integration requirements (API vs. batch file)

---

## 8. Risk Mitigations

| Risk | Mitigation |
|---|---|
| Landscape quality too low for useful extraction | Pre-build validation gate with rubric scoring; anchor on org positions from CMS; iterative prompt refinement before gate evaluation |
| Extraction accuracy insufficient | Early 50-message benchmark; confidence gating at 0.7 filters low-quality signals; audit dashboard enables rapid iteration |

---

## 9. Explicitly Deferred (NOT in This Stage)

The following are deferred to later stages. Do not implement them here:

- User-facing opinion elicitation or position prompting
- Consent prompts or data usage disclosures
- Memberstack account creation or login
- Polis integration or seed statement generation
- Opinion clustering or consensus detection
- Position maps or opinion visualizations on the website
