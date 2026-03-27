# Jigsaw: VoteBot Opinion Elicitation System — Overview

## Vision

VoteBot becomes a **guided opinion elicitation tool** for legislation. When users chat about a bill, VoteBot doesn't just answer questions — it understands the landscape of policy positions on that bill and helps users articulate where they stand. Their opinions feed a shared opinion space alongside direct Polis voters, producing a living map of public sentiment on every tracked bill.

**What makes this different from a survey:** The conversation is natural. Users don't see a form — they chat. VoteBot surfaces relevant policy positions contextually, when the conversation naturally turns to a topic. Users can push back, add nuance, or raise concerns the system hasn't seen before. The elicitation feels like a conversation with a knowledgeable person, not a questionnaire.

### The Core Principle

> **Conversation drives → structure assists → never the reverse.**

Structured questions (options, clarifications) are tools for fixing ambiguity and increasing density. They never drive the experience. If the system starts feeling like a survey, it's broken.

### Long-Term Goal: From Opinion to Legislation

```
Voter conversations → Opinion vectors → Clustering & consensus
    → Policy directives → Draft amendments / new legislation
```

This rollout covers the first three stages — opinion elicitation, clustering, and reporting results on the DDP website. The legislative drafting stage is not yet specified (strictly internal roadmap) and will depend on emerging tools. Every design decision is made with the drafting stage in mind: opinion stances trace back to specific bill sections, novel claims capture what voters want that the bill doesn't address, and the data model is tool-agnostic.

### What This System Is NOT

- **Not a poll.** The sample is self-selected and engaged, not representative of the electorate. All public reporting must state this.
- **Not a survey.** Users chat naturally; structure assists but never drives.
- **Not voting.** Opinions are expressed, not ballots cast. The system maps sentiment, not electoral outcomes.

---

## Core Model: Multi-Position Opinion Vectors

Each topic in a bill has 3-6 **policy positions** — distinct stances that real people hold. Each position is one dimension of the opinion vector. A user's opinion is encoded as their agreement/disagreement with each position:

```
Bill: HB 123 (Education Reform)

Topic: Funding Approach
  P1: "Fund through the bill's proposed state formula"
  P2: "Tie funding to local property tax base instead"
  P3: "Fund through reallocation, not new revenue"
  P4: "Reduce overall education spending"

User's opinion vector:
  P1=+0.8, P2=-0.3, P3=+0.5, P4=0, ...
```

Positions aren't mutually exclusive. PCA finds the real axes of disagreement, which might not align with any single topic. Each policy position maps to a Polis seed statement — same coordinate space, different granularity (Polis: discrete +1/-1/0, Chat: continuous).

### Landscape Generation

The opinion landscape is **semi-structured, not fully generative**. Seeded with known dimensions (org positions from Webflow CMS, canonical policy dimensions), the LLM expands within constraints. This prevents the opinion space from being purely probabilistic — it's anchored to real-world advocacy positions.

- **Stage A:** Simplified landscapes (org positions + bill summary + canonical dimensions)
- **Stage B:** Full LLM-generated landscapes with embedding deduplication and org enrichment

---

## Staged Rollout Strategy

Each stage delivers user-visible value independently and validates assumptions before the next stage depends on them. **Do not proceed to the next stage until validation gates pass.**

| Stage | Document | Goal | Key Validation | Status |
|---|---|---|---|---|
| **A** | [PLAN-jigsaw-stage-a.md](PLAN-jigsaw-stage-a.md) | Personalization + silent opinion extraction | Can we extract opinions? Do users express them? | Ready for implementation |
| **B** | [PLAN-jigsaw-stage-b.md](PLAN-jigsaw-stage-b.md) | Guided elicitation with 5 modes + orchestration | Do users engage with opinion features? | Blocked on Stage A |
| **C** | [PLAN-jigsaw-stage-c.md](PLAN-jigsaw-stage-c.md) | Accounts, consent, voter verification, cost control | Will users create accounts and verify? | Blocked on Stage B |
| **D** | [PLAN-jigsaw-stage-d.md](PLAN-jigsaw-stage-d.md) | Polis clustering, opinion maps, public reporting | Are cluster results meaningful and trusted? | Blocked on Stage C |
| **E** | [PLAN-jigsaw-stage-e.md](PLAN-jigsaw-stage-e.md) | Emergent positions, full loop, future drafting | Does the system improve itself safely? | Blocked on Stage D |

### The One Hard KPI

> **% of bill-page conversations with ≥3 high-confidence positions extracted**

If this number is low, nothing else matters. Measured as a tiered KPI:
- **Entry (≥1 position):** >25% — users express at least some opinion
- **Predictive (≥2 high-confidence):** >15% — most realistic predictor of downstream success
- **Useful (≥3 positions):** >10% minimum survival / 20-30% real confidence

**Primary cohort: first-time users.** Returning users with personalized experiences may inflate metrics.

### Success Criteria by Stage

| Metric | Target | Stage |
|---|---|---|
| Queries per session | +20% vs. baseline | A |
| Bill-page conversations with opinion language | >15% | A |
| **Conversations with 3+ positions extracted** | **>10% (survival) / 20-30% (confidence)** | **A** |
| Extraction recall (should-have-extracted) | >60% minimum / 70-75% real target | A |
| Extraction resolution (correct specificity) | >70% | A |
| Inline confirmation acceptance | >50% | B |
| Structured clarification acceptance | >40% | B |
| "Add your voice" completion (7+ positions) | >20% of prompted | B |
| Free-text override rate | <40% (higher = landscape needs work) | B |
| Drop-off after structured prompt | <15% increase vs baseline | B |
| Users create accounts | >5% of opinion-expressing visitors | C |
| Cluster results match human intuition | >70% agreement | D |
| Cluster stability (10% removed) | >85% same assignment | D |
| Users return to check opinion maps | >10% return rate | D |

### Go/No-Go Gates

| Gate | When | Criteria | If Fail |
|---|---|---|---|
| Landscape quality | After 10 bills (incl. 2 messy) | Avg rubric >= 3.5/5 | Iterate prompts; do not proceed to Stage B |
| Extraction accuracy | After 50-message audit | Kappa >= 0.6 AND recall >= 60% | Rethink extraction; consider explicit-only |
| Latent opinion density | After 50 conversations | Avg >= 2 extractable opinions/conversation | If low: bottleneck is user behavior, not extraction |
| Elicitation engagement | Stage B live data | >30% prompt acceptance, >20% reach 7+ | If low: elicitation UX needs rework |
| Cluster validity | First 3 bills at 30+ participants | >70% human intuition match + >85% stability | If unstable: simplify to per-position aggregates |

---

## Identity Model

| Level | ID | Storage | Lifetime | Purpose |
|---|---|---|---|---|
| **Visitor** | `visitor_id` | `localStorage` | Permanent (best-effort) | Cross-session device tracking (implemented) |
| **Session** | `session_id` | `sessionStorage` | Per-tab, 30-min timeout | Conversation continuity (implemented) |
| **Conversation** | `conversation_id` | Server-side | Resets on boundary | Multi-turn behavior (implemented) |
| **Member** | `member_id` | Memberstack | Permanent, cross-device | Durable opinion storage (Stage C) |
| **Verified Voter** | `catalist_dwid` | PostgreSQL + Memberstack | Permanent | District attribution, anti-gaming (Stage C) |

**Onboarding is entirely conversational** — VoteBot asks for name + email (Step A → Memberstack account via Admin API), then optionally DOB + zip (Step B → Catalist voter verification). No modals, no redirects, no password upfront. The server creates the identity; the client never provides a bare `member_id`.

### Participation Levels — Product Language

Every user-facing number must be labeled with its actual meaning. **Never conflate aggregate counts with cluster inclusion.**

| Level | What It Means | Shown As |
|---|---|---|
| **Discussions** | Chatted about a bill, expressed at least one opinion-like statement | "312 people have discussed this bill" |
| **Confirmed opinions** | Reviewed extracted stances and confirmed | "187 people confirmed their views" |
| **Cluster participants** | 7+ confirmed stances, vector confidence > 0.5, included in PCA/clustering | "124 people included in the opinion map" |
| **Verified voters** | Cluster participant with Catalist DWID match | "89 verified voters in this district" |

**Anti-patterns (never do this):**
- "312 people agree that..." (conflates discussion with agreement)
- "Most people think..." (unquantified, implies representativeness)
- "Voters in your district support..." (without sample size or verification level)
- "Three groups have emerged" at <100 participants (too ontologically strong — use "three recurring patterns appear")

### Cluster Language by Sample Size

| Participants | Language Tier | Example |
|---|---|---|
| < 10 | No data | "Not enough people have shared views yet" |
| 10-29 | Aggregates only | "Of 23 people who weighed in, 74% support..." |
| 30-99 | **Early signal** | "Among 67 people, we're seeing three recurring patterns..." |
| 100+ | Established | "312 people have weighed in. Here's what we're seeing..." |

### District Reporting Thresholds

| Verified Voters | Display |
|---|---|
| < 15 | Hidden |
| 15-29 | "Early district signal" badge + per-position aggregates only |
| 30-49 | Provisional cluster distribution with margin of error |
| 50+ | Full district reporting with statewide comparison |

---

## Elicitation Architecture

### Five Modes + Orchestration

| Mode | Trigger | Purpose |
|---|---|---|
| **1: Passive** | Every message | Silent extraction, no user interruption |
| **1.5: Confirmation** | Confidence >= 0.65, max 2/session | Fix high-confidence errors early |
| **2: Contextual** | User discusses a landscape topic | Natural expansion of coverage |
| **2.5: Structured Clarification** | Confidence < 0.6 or ambiguity | Resolve unclear opinions with options + "something else" |
| **3: "Add Your Voice"** | 2+ positions + positive engagement | Civic participation framing to reach 7+ positions |

### Orchestration (decision engine, priority order)

1. Always run Mode 1 (passive)
2. If hesitation detected → disable all elicitation for session
3. If cooldown active (< 2 turns since last prompt) → wait
4. If session cap reached (2 structured prompts) → stop
5. If high-confidence extraction → consider Mode 1.5
6. If ambiguity → consider Mode 2.5
7. If natural expansion possible → consider Mode 2
8. If 2+ positions + engaged → consider Mode 3

### Hard Guardrails

- Never 2 structured prompts in a row
- Max 2 structured prompts per session (reduced from 3)
- Never without conversational context
- Always allow skip and free-text override ("something else" is mandatory)
- Stop immediately on hesitation signals

---

## System Architecture

```
    DDP Website — Bill Page
    +--------------------------------------------+
    |                                            |
    |  +------------------+  +----------------+  |
    |  | VoteBot Chat     |  | Polis Embed    |  |
    |  | (primary input)  |  | (optional, E)  |  |
    |  +--------+---------+  +-------+--------+  |
    +-----------|--------------------|-----------+
                |                    |
                v                    v
         VoteBot API            Polis API
              |                    |
              v                    v
      Opinion Extraction     Vote Matrix
      (5 elicitation modes)  (+1/-1/0 per stmt)
              |                    |
              v                    v
    +---------+--------------------+---------+
    |     Multi-Position Opinion Vectors     |  ← PostgreSQL (durable)
    |     (participants x positions)         |  ← Redis (hot cache)
    +-------------------+--------------------+
                        |
                        v
                  PCA / UMAP / K-means         ← Polis Clojure math
                        |                      ← Delphi narratives
                        v
                  Opinion Clusters + Narratives
                        |
              +---------+---------+-----------------+
              v                   v                 v
        VoteBot responses    Polis visualization    DDP Website
        ("patterns show...")  (PCA opinion map)     (opinion reports)
                                                    |
                                            - - - - v - - - - - -
                                            : [FUTURE - INTERNAL]:
                                            : Legislative        :
                                            : Drafting Service   :
                                            - - - - - - - - - - -
```

---

## Storage Architecture

| System | Role | Durability |
|---|---|---|
| **Redis** | Hot cache — sessions, opinion vectors under construction, rate limits, budgets | Volatile (TTL-based). Budgets persisted to PostgreSQL every 15 min. |
| **PostgreSQL (RDS)** | Durable storage — opinion vectors, signals, landscapes, verified voters, clustering snapshots, migrations, budget audit | Permanent. Existing DDP RDS instance. |
| **Polis PostgreSQL** | Vote storage — discretized votes for math pipeline | Permanent. Separate instance. |
| **DynamoDB** | Delphi output — clustering results, narrative reports | Permanent. VoteBot reads only (consumer, not owner). |
| **Memberstack** | Member metadata — DWID, districts, verified status | Permanent. |

### PostgreSQL Tables by Stage

| Stage | Tables Introduced |
|---|---|
| A | `opinion_landscapes`, `opinion_signals` (with `persisted` flag) |
| B | `landscape_migrations` |
| C | `opinion_vectors`, `verified_voters`, `budget_snapshots` |
| D | `clustering_snapshots`, `weight_calibration_log` |

---

## Cost Control Architecture

8-layer defense system. Not all layers activate at once — they roll out with the stages.

| Layer | Stage | What It Does |
|---|---|---|
| 1. Per-session budget | A+ | Caps tokens per conversation by identity tier |
| 2. Per-user daily budget | C | Rolling 24h window by composite identity |
| 3. Rate limiting + abuse detection | A+ | 5 req/10s, behavioral bot detection, per-IP daily cap |
| 4. Model routing | A+ | Cheap models for extraction/classification (50-80% savings) |
| 5. Budget-aware response downgrade | B+ | Fallback to cheaper model when budget runs low |
| 6. Identity-as-incentive | C | Cost limits become conversion funnel |
| 7. Cost monitoring dashboard | A+ | Tokens/day, cost/user, cost per meaningful outcome |
| 8. Kill switch (velocity + daily) | A+ | Proactive (5-min spend rate) + reactive (daily threshold) |

**Key principle:** Control cost by controlling usage, not by restricting access. Identity is a benefit unlock, not a gate.

---

## Risk Summary

| Risk | Severity | Mitigation | Stage |
|---|---|---|---|
| Landscape quality untested | Critical | Pre-build validation of 10 bills (incl. 2 messy), rubric scoring | A |
| Extraction accuracy unproven | High | Precision + recall + resolution audit, kappa >= 0.6 | A |
| Confidence threshold creates bias | Medium | Log ALL signals, persist >= 0.7 only, monitor distribution | A |
| 7-vote participation cliff | High | "Add your voice" prompt + cross-session accumulation | B |
| Elicitation feels like a survey | High | Orchestration guardrails, hesitation detection, max 2 structured prompts | B |
| Runaway AI token costs | High | 8-layer cost control, model routing, kill switch | A-C |
| Catalist external dependency | Medium | Start procurement in parallel; not on PMF critical path | C |
| Cluster results overclaim | High | Tiered language, inline caveats, "early signal" badges, sample sizes | D |
| Delphi narratives hallucinate sociologically | High | Narratives must be grounded in positions + quotes, labels are editorial | D |
| Feedback loops reinforce system framing | High | Counterfactual elicitation, emergence monitoring, position lineage | E |
| Emergent positions degrade interpretability | Medium | Conservative caps, diversity constraints, coverage decay monitor | E |

---

## Key Design Decisions (Resolved)

| Decision | Resolution | Rationale |
|---|---|---|
| Sparsity strategy | "Add your voice" minimum elicitation depth | Active guidance, not threshold lowering or synthetic completion |
| Durable storage | PostgreSQL (existing RDS) | Relational queries for district aggregation; future drafting joins |
| Anonymous cost control identity | `hash(visitor_id + IP + UA)` | Prevents bypass via cookie clearing |
| Missing values in PCA | Masked (not zero-imputed) | Prevents sparse users being pulled toward center |
| Cluster routing | Component floors + composite score | Coverage >= 0.15 AND avg_confidence >= 0.5 before composite applies |
| Onboarding UX | Entirely in-chat, server-side | No modals, no redirects; server creates Memberstack account |
| Consent timing | After 3+ opinion signals + natural pause | One prompt per session; decline is permanent for session |
| Weights | Provisional heuristics (0.85/0.7/0.5) | Calibrated empirically after 200+ opinions; methodology states this |
| District reporting minimum | 15 verified voters (early signal only) | Full reporting at 50+; comparative claims require 50+ |
| Emergent position posture | Conservative v1 (max 3-5/week, diversity constraint) | Expand automation only after observing real patterns |
| Legislative drafting | Strictly internal roadmap | No external communication until opinion system validated at scale |

---

## Epistemic Honesty

This system makes claims about what people think. That changes the standard.

**The math is not the biggest risk. The narrative layer is.**

Every public-facing output must communicate:
1. **Sample size** — how many people contributed
2. **Selection bias** — self-selected engaged participants, not a representative poll
3. **Confidence level** — early signal vs. established vs. verified
4. **Freshness** — when the data was last computed
5. **Limitations** — what clusters are (mathematical partitions) and what they aren't (sociological facts)

Clusters are not "groups of opinion that exist in reality." They are "patterns the algorithm found in the data from people who chose to participate." The language must never imply more than that.

> **The system is not just measuring opinion — it is shaping the space of possible opinions.** Users express views → system creates positions → future users select from them → those selections reinforce positions. This feedback loop is unavoidable but must be monitored and managed.

---

## Related Documents

| Document | What It Covers |
|---|---|
| [PLAN-jigsaw-stage-a.md](PLAN-jigsaw-stage-a.md) | Personalization + silent extraction + cost basics |
| [PLAN-jigsaw-stage-b.md](PLAN-jigsaw-stage-b.md) | 5-mode elicitation + orchestration + stance history |
| [PLAN-jigsaw-stage-c.md](PLAN-jigsaw-stage-c.md) | Memberstack accounts + Catalist verification + full cost control |
| [PLAN-jigsaw-stage-d.md](PLAN-jigsaw-stage-d.md) | Polis clustering + opinion maps + epistemic safeguards |
| [PLAN-jigsaw-stage-e.md](PLAN-jigsaw-stage-e.md) | Emergent positions + feedback loop monitoring + future drafting |
| [PLAN-polis-math-pipeline.md](PLAN-polis-math-pipeline.md) | Technical reference: Polis math pipeline data flow |
| [user-analytics-logging.md](user-analytics-logging.md) | Implemented: event-based analytics system |
| [PLAN-user-personalization.md](PLAN-user-personalization.md) | Superseded: identity pipeline (absorbed into stages) |
| [PLAN-votebot-polis-jigsaw.md](PLAN-votebot-polis-jigsaw.md) | Superseded: original monolithic plan (retained as reference) |
