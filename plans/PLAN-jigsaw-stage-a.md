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
- [ ] Gate on confidence: only persist signals with confidence >= 0.7 to the opinion vector
- [ ] **Log ALL signals regardless of confidence** — low-confidence signals are written to `opinion_signals` with `persisted: false` for debugging and recall analysis. Only signals >= 0.7 feed into the opinion vector.
- [ ] Feature flag: `JIGSAW_SILENT_EXTRACTION_ENABLED` (default: false)

### Extraction Audit Dashboard

- [ ] Build a review script or Jupyter notebook that:
  - Pulls extracted stances from `opinion_signals` (including `persisted = false` low-confidence signals)
  - Displays them alongside the source user messages
  - Allows manual judgment: **correct / partially correct / incorrect**
  - Also labels **false negatives**: messages where a human would have extracted an opinion but the system didn't ("should-have-extracted")
  - Computes agreement metrics (Cohen's kappa) for precision
  - Computes **recall proxy**: % of "should-have-extracted" messages that the system actually extracted
- [ ] Use this for the 50-message validation gate (see below)

### Opinion Taxonomy (what counts as extractable)

For consistent measurement across auditors, define what qualifies as an extractable opinion signal:

| Category | Example | Extractable? |
|---|---|---|
| **Explicit stance** | "I support this bill's funding formula" | Yes — high confidence |
| **Implicit stance** | "The funding formula seems reasonable" | Yes — moderate confidence |
| **Concern** | "I'm worried about the enforcement timeline" | Yes — maps to opposition on timeline positions |
| **Preference** | "I'd rather see a 5-year rollout" | Yes — maps to specific position |
| **Question implying stance** | "Why didn't they include inflation indexing?" | Maybe — depends on context, low confidence |
| **Pure information-seeking** | "What does this bill do?" | No — no opinion signal |
| **Vague sentiment** | "This bill is interesting" | No — no extractable position |

This taxonomy ensures the kappa score and recall measurement are consistent across auditors.

### User Intent Classification

- [ ] Classify each bill-page conversation as **opinion-seeking** or **info-seeking** based on the message content
- [ ] Track extraction rates separately for each intent category
- [ ] If extraction rate is low, distinguish between "system failure" (opinion expressed but not extracted) and "user intent" (user only wanted information)
- [ ] Use the existing `sub_intent` field to approximate: `summary`, `explanation`, `navigation` → info-seeking; `support_opposition`, `vote_history` → opinion-seeking

### Analytics Integration

- [ ] Ensure `query_processed` events with `primary_intent: bill` trigger extraction
- [ ] Log extraction results as analytics events (signal count, confidence distribution)
- [ ] Track extraction latency separately from response latency
- [ ] **Segment all extraction metrics by user type:**
  - First-time visitors vs. returning visitors (personalization may change behavior)
  - Short sessions vs. long sessions
  - Bill page vs. general page
- [ ] **Track engagement drop-off after opinion language:** if a user expresses an opinion then stops chatting, log it as a signal that something may be wrong
- [ ] Track opinion prevalence by intent category (opinion-seeking vs info-seeking conversations)

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
| `persisted` | BOOLEAN DEFAULT TRUE | Whether the signal met the confidence threshold (>= 0.7) and was used in the opinion vector. Low-confidence signals are stored with `persisted = false` for debugging and recall analysis. |
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

- [ ] Generate simplified landscapes for **10 real bills**, including at least **2 "messy" bills** with broader, more complex position spaces (e.g., omnibus bills, multi-topic legislation). Stage B will expand to full LLM-generated landscapes — the messy bills test whether extraction degrades with more positions.
- [ ] Human-evaluate each landscape on a 1-5 rubric (dimensions: completeness, accuracy, position clarity, org alignment)
- [ ] **Go/no-go: average score >= 3.5/5**

### Gate 2: Extraction Accuracy (Precision + Recall + Resolution)

- [ ] Run extraction on **50 real production messages** from bill pages
- [ ] Two human raters independently judge each extraction on three dimensions:
  - **Precision**: Was the extraction correct? (correct / partially correct / incorrect)
  - **Resolution**: Was the extraction at the right level of specificity? (too coarse / correct / overly specific). Example: "supports housing reform" is too coarse; "supports density with local design authority" is correct.
  - **Recall**: Was there an opinion the system should have extracted but didn't? (should-have-extracted label)
- [ ] Raters use the **recall labeling rubric** (see below) with canonical examples for consistency
- [ ] Compute inter-rater agreement using **Cohen's kappa**
- [ ] Compute **signal-to-noise ratio**: correct extractions / total extractions
- [ ] **Go/no-go: kappa >= 0.6 on precision AND recall proxy >= 60% (minimum) / 70-75% (real target)**. Passing at 60% recall means 40% of opinions are missed — acceptable for Stage A but must improve before Stage B.

#### Recall Labeling Rubric

Strict definition of "should-have-extracted" with canonical examples. Both auditors must use this rubric to ensure recall measurement is consistent, not subjective.

| Message | Should Extract? | Mapped Position | Why |
|---|---|---|---|
| "I support this bill's funding formula" | Yes | funding_formula: +0.9 | Explicit stance, direct |
| "The timeline seems too aggressive" | Yes | timeline_3yr: -0.6 | Implicit opposition, clear concern |
| "I'm worried about enforcement" | Yes | enforcement: -0.4 (low confidence) | Concern implying stance |
| "I'd rather see a 5-year rollout" | Yes | timeline_5yr: +0.8 | Explicit preference for specific position |
| "Why didn't they include inflation indexing?" | Maybe | funding_indexing: +0.3 (low confidence) | Question implying stance — extract at low confidence |
| "What does this bill do?" | No | — | Pure info-seeking |
| "This bill is interesting" | No | — | Vague sentiment, no extractable position |
| "I'm not sure how I feel about this" | No | — | Explicit uncertainty, not an opinion |
| "My neighbor thinks this is terrible" | No | — | Third-party attribution, not user's opinion |
| "I can see both sides on enforcement" | Maybe | — | Ambivalence — extract as low-confidence mixed signal if at all |

Provide at least **15 labeled examples** covering edge cases before the audit begins. Calibrate raters on the first 10 messages together before independent scoring.

### Gate 3: Opinion Prevalence

- [ ] Measure: what percentage of bill-page messages contain extractable opinion language?
- [ ] Report the distribution, segmented by user intent (opinion-seeking vs info-seeking)
- [ ] Informational, no hard gate — but informs Stage B design

### Gate 4: Extraction Depth (Tiered KPI)

Track extraction depth at four tiers:

| Tier | Definition | Target | What It Means |
|---|---|---|---|
| Entry | >= 1 position extracted | > 25% of bill-page conversations | Users express at least some opinion |
| **Predictive** | **>= 2 high-confidence positions** | **> 15% of bill-page conversations** | **Most realistic predictor of Stage B success** |
| Engagement | >= 2 positions extracted (any confidence) | > 15% of bill-page conversations | Users express opinions on multiple topics |
| **Useful** | **>= 3 positions extracted** | **> 10% of bill-page conversations** | **Enough signal for a partial opinion vector** |

- [ ] **Go/no-go: "Useful" tier > 10%** (minimum survival threshold)
- [ ] **Internal target: "Useful" tier > 20-30%** for genuine confidence that the system will work at scale. Passing at 10% means "this works for a subset of users" — not "this works."
- [ ] **Primary cohort: first-time users.** If KPIs only pass for returning users (who have personalized experiences), the system may not generalize. Report first-time users as the primary number, returning users as secondary.
- [ ] Report all four tiers segmented by first-time vs. returning visitors

### Gate 5: Opinion Density (New)

Measures how many opinions were *available* to extract — not just how many were extracted. This distinguishes "extraction failure" from "users don't express enough opinions."

- [ ] For a sample of 50 bill-page conversations, human raters count the total number of extractable opinion signals per conversation (using the recall rubric)
- [ ] Compute **latent opinion density**: average extractable opinions per conversation
- [ ] If density is < 2 per conversation, the bottleneck is **user behavior** (they don't express enough opinions), not extraction quality. Stage B's elicitation prompts are the solution.
- [ ] If density is >= 3 but extracted positions are < 2, the bottleneck is **extraction quality**. Improve extraction before moving to Stage B.

---

## 6. Success Metrics

| Metric | Baseline | Target | Measurement |
|---|---|---|---|
| Queries per session | Current avg | **+20% vs baseline** | Analytics: query count per session_id |
| Bill-page conversations with >= 1 position (entry) | Unknown | **> 25%** | Extraction pipeline |
| Bill-page conversations with >= 2 high-confidence positions (predictive) | Unknown | **> 15%** | Extraction pipeline |
| **Bill-page conversations with >= 3 positions (useful)** | Unknown | **> 10% (survival) / 20-30% (real confidence)** | Extraction pipeline |
| Extraction recall (should-have-extracted) | Unknown | **> 60% (minimum) / 70-75% (real target)** | Audit loop with recall rubric |
| Extraction resolution (correct specificity) | Unknown | **> 70% "correct" resolution** | Audit loop: coarse/correct/overly-specific rating |
| Signal-to-noise ratio | Unknown | **> 75%** | Correct extractions / total extractions |
| Latent opinion density | Unknown | **> 2 extractable opinions per conversation** | Human-rated sample of 50 conversations |
| Engagement drop-off after opinion language | Unknown | **No significant increase vs. non-opinion sessions** | Baseline comparison (see below) |

**Primary cohort for all metrics: first-time users.** Returning users are secondary — personalization may inflate their engagement.

**Drop-off definition:** Compare session continuation rate (% of users who send another message) for conversations where opinion language was detected vs. conversations where no opinion language was detected. A "significant increase" is > 10 percentage points difference. This requires the baseline comparison — not just absolute drop-off numbers.

---

## 7. Parallel Workstream: Catalist Procurement

This is the **longest lead-time item** in the Jigsaw roadmap and is NOT on the critical path for Stage A. Start it now so it is ready when needed in later stages.

- [ ] Initial outreach to Catalist
- [ ] Review data sharing agreement and terms
- [ ] Specify voter file fields and matching workflow needed for Stage C
- [ ] Identify technical integration requirements (API vs. batch file)

---

## 8. Cost Controls (Stage A Subset)

Stage A introduces LLM extraction calls on every bill-page message — the first new cost surface. The full cost control architecture is defined in [PLAN-jigsaw-stage-c.md](PLAN-jigsaw-stage-c.md) Section 12. Stage A implements:

- [ ] **Per-session token limit** (Layer 1): Cap anonymous sessions at ~10k tokens. Track in Redis `votebot:session_budget:{session_id}`.
- [ ] **Rate limiting** (Layer 3): Max 5 requests/10s, 20 requests/min per visitor_id/IP. Redis-based.
- [ ] **Model routing** (Layer 4): Use cheap model (Haiku/4o-mini) for opinion extraction. Keep GPT-4.1 for user-facing responses only. Extraction is the highest-volume new call — routing it cheap reduces cost by 50-80%.
- [ ] **Kill switch** (Layer 8): If daily spend exceeds threshold, reduce extraction to sampling (every 3rd message) and alert admin.
- [ ] **Cost tracking**: Log tokens used per extraction call in analytics events. Monitor daily cost per user.

Tiered daily budgets (Layer 2) and identity-based incentives (Layer 6) come in Stage C when Memberstack accounts enable per-user tracking.

---

## 9. Risk Mitigations

| Risk | Mitigation |
|---|---|
| Landscape quality too low for useful extraction | Pre-build validation gate with rubric scoring; include 2 messy bills; anchor on org positions from CMS; iterative prompt refinement |
| Extraction accuracy insufficient | 50-message benchmark with both precision and recall; confidence gating at 0.7 for persistence; ALL signals logged for debugging |
| Confidence threshold creates hidden bias toward polarized opinions | Log low-confidence signals with `persisted: false`; monitor confidence distribution; compare high-vs-low confidence extractions in audit |
| False negatives go undetected | "Should-have-extracted" labeling in audit loop; recall proxy computed alongside kappa; false negatives are as important as false positives |
| Personalization contaminates extraction metrics | Segment all metrics by first-time vs. returning visitors; report both cohorts in validation gates |
| Passing at 10% gives false confidence | Tiered KPI (entry/engagement/useful); internal target 20-30%; 10% is minimum survival, not success |
| Low extraction rate is ambiguous (system failure vs. user intent) | Opinion-seeking vs. info-seeking classification; extraction rates reported per intent category |

---

## 10. Explicitly Deferred (NOT in This Stage)

The following are deferred to later stages. Do not implement them here:

- User-facing opinion elicitation or position prompting
- Consent prompts or data usage disclosures
- Memberstack account creation or login
- Polis integration or seed statement generation
- Opinion clustering or consensus detection
- Position maps or opinion visualizations on the website
