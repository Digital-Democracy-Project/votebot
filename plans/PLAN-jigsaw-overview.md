# Jigsaw: VoteBot Opinion Elicitation System — Overview

## Vision

VoteBot becomes a **guided opinion elicitation tool** for legislation. When users chat about a bill, VoteBot doesn't just answer questions — it understands the landscape of policy positions on that bill and helps users articulate where they stand. Their opinions feed a shared opinion space alongside direct Polis voters, producing a living map of public sentiment on every tracked bill.

**What makes this different from a survey:** The conversation is natural. Users don't see a form — they chat. VoteBot surfaces relevant policy positions contextually, when the conversation naturally turns to a topic. Users can push back, add nuance, or raise concerns the system hasn't seen before.

### Long-Term Goal: From Opinion to Legislation

```
Voter conversations → Opinion vectors → Clustering & consensus
    → Policy directives → Draft amendments / new legislation
```

This rollout covers the first three stages — opinion elicitation, clustering, and reporting results on the DDP website. The legislative drafting stage is not yet specified and will depend on emerging tools. Every design decision is made with the drafting stage in mind.

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

---

## Staged Rollout Strategy

Each stage delivers user-visible value independently and validates assumptions before the next stage depends on them. **Do not proceed to the next stage until validation gates pass.**

| Stage | Document | Goal | Key Validation |
|---|---|---|---|
| **A** | [PLAN-jigsaw-stage-a.md](PLAN-jigsaw-stage-a.md) | Personalization + silent opinion extraction | Can we extract opinions? Do users express them? |
| **B** | [PLAN-jigsaw-stage-b.md](PLAN-jigsaw-stage-b.md) | Positions in chat, "add your voice" prompt | Do users engage with opinion features? |
| **C** | [PLAN-jigsaw-stage-c.md](PLAN-jigsaw-stage-c.md) | Accounts, consent, voter verification | Will users create accounts and verify? |
| **D** | [PLAN-jigsaw-stage-d.md](PLAN-jigsaw-stage-d.md) | Polis clustering, opinion maps on website | Are cluster results meaningful and trusted? |
| **E** | [PLAN-jigsaw-stage-e.md](PLAN-jigsaw-stage-e.md) | Emergent positions, full loop, future drafting | Does the system improve itself? |

### The One Hard KPI

The single most important metric across all stages:

> **% of bill-page conversations with ≥3 high-confidence positions extracted**

If this number is low, nothing else matters. Clustering, Polis, verification — all depend on users expressing enough structured opinions through conversation.

### Success Criteria by Stage

| Metric | Target | Stage |
|---|---|---|
| Users engage more (queries per session) | +20% vs. baseline | A |
| Users express opinions in chat | >15% of bill-page conversations contain opinion language | A |
| **Conversations with 3+ positions extracted** | **>10% of bill-page conversations** | **A** |
| Inline correction acceptance | >50% of users respond to "Did I get that right?" | B |
| Users interact with position prompts | >30% acceptance rate on contextual prompts | B |
| Users complete "add your voice" flow | >20% of prompted users reach 7+ positions | B |
| Users create accounts to save opinions | >5% of opinion-expressing visitors | C |
| Cluster results match human intuition | >70% agreement in qualitative review | D |
| Users return to check opinion maps | >10% return visitor rate on opinion report pages | D |

---

## Identity Model

| Level | ID | Storage | Lifetime | Purpose |
|---|---|---|---|---|
| **Visitor** | `visitor_id` | `localStorage` | Permanent (best-effort) | Cross-session device tracking (implemented) |
| **Session** | `session_id` | `sessionStorage` | Per-tab, 30-min timeout | Conversation continuity (implemented) |
| **Conversation** | `conversation_id` | Server-side | Resets on boundary | Multi-turn behavior (implemented) |
| **Member** | `member_id` | Memberstack | Permanent, cross-device | Durable opinion storage (Stage C) |
| **Verified Voter** | `catalist_dwid` | PostgreSQL + Memberstack | Permanent | District attribution, anti-gaming (Stage C) |

### Participation Levels — Product Language

These must be distinguished clearly in all user-facing language:

| Level | What It Means | Shown As |
|---|---|---|
| **Discussions** | Chatted about a bill, expressed at least one opinion-like statement | "312 people have discussed this bill" |
| **Confirmed opinions** | Reviewed extracted stances and confirmed | "187 people confirmed their views" |
| **Cluster participants** | 7+ confirmed stances, included in PCA/clustering | "124 people included in the opinion map" |
| **Verified voters** | Cluster participant with Catalist DWID match | "89 verified voters in this district" |

---

## System Architecture

```
    DDP Website — Bill Page
    +--------------------------------------------+
    |                                            |
    |  +------------------+  +----------------+  |
    |  | VoteBot Chat     |  | Polis Embed    |  |
    |  | (primary input)  |  | (optional)     |  |
    |  +--------+---------+  +-------+--------+  |
    +-----------|--------------------|-----------+
                |                    |
                v                    v
         VoteBot API            Polis API
              |                    |
              v                    v
      Opinion Extraction     Vote Matrix
      + Position Matching    (+1/-1/0 per stmt)
              |                    |
              v                    v
    +---------+--------------------+---------+
    |     Multi-Position Opinion Vectors     |  ← PostgreSQL (durable)
    |     (participants x positions)         |  ← Redis (hot cache)
    +-------------------+--------------------+
                        |
                        v
                  PCA / UMAP / K-means         ← Polis Clojure math
                        |                      ← Delphi enhancement
                        v
                  Opinion Clusters + Narratives
                        |
              +---------+---------+-----------------+
              v                   v                 v
        VoteBot responses    Polis visualization    DDP Website
        ("others think...")  (PCA opinion map)      (opinion reports)
                                                    |
                                            - - - - v - - - - - -
                                            : [FUTURE]          :
                                            : Legislative       :
                                            : Drafting Service  :
                                            - - - - - - - - - - -
```

---

## Storage Architecture

| System | Role | Durability |
|---|---|---|
| **Redis** | Hot cache — active sessions, opinion vectors under construction | Volatile (TTL-based) |
| **PostgreSQL (RDS)** | Durable storage — opinion vectors, signals, landscapes, verified voters | Permanent. Existing DDP RDS instance. |
| **Polis PostgreSQL** | Vote storage — discretized votes for math pipeline | Permanent. Separate instance. |
| **DynamoDB** | Delphi output — clustering results, narrative reports | Permanent. VoteBot reads only. |
| **Memberstack** | Member metadata — DWID, districts, verified status | Permanent. |

See individual stage documents for the specific PostgreSQL tables and Redis keys introduced in each stage.

---

## Risk Summary

| Risk | Severity | Mitigation | Stage Addressed |
|---|---|---|---|
| Opinion landscape quality untested | Critical | Pre-build validation of 10 bills, go/no-go at 3.5/5 | A |
| Passive extraction accuracy unproven | High | 50-message kappa benchmark, go/no-go at 0.6 | A |
| 7-vote participation cliff | High | "Add your voice" prompt + cross-session accumulation | B |
| Catalist external dependency | Medium | Start procurement in parallel; not on PMF critical path | C |
| No governance for wrong results | Medium | Methodology page, confidence intervals, audit trail | D |
| Emergent position drift | Medium | Conservative v1 posture, snapshot isolation | E |

Full risk mitigations are documented in each stage's plan.

---

## Related Documents

| Document | What It Covers |
|---|---|
| [PLAN-jigsaw-stage-a.md](PLAN-jigsaw-stage-a.md) | Personalization + silent extraction |
| [PLAN-jigsaw-stage-b.md](PLAN-jigsaw-stage-b.md) | Guided elicitation + "add your voice" |
| [PLAN-jigsaw-stage-c.md](PLAN-jigsaw-stage-c.md) | Memberstack accounts + Catalist voter verification |
| [PLAN-jigsaw-stage-d.md](PLAN-jigsaw-stage-d.md) | Polis integration + clustering + opinion maps |
| [PLAN-jigsaw-stage-e.md](PLAN-jigsaw-stage-e.md) | Emergent positions + full loop + future drafting |
| [PLAN-polis-math-pipeline.md](PLAN-polis-math-pipeline.md) | Technical reference: Polis math pipeline data flow |
| [user-analytics-logging.md](user-analytics-logging.md) | Implemented: event-based analytics system |
| [PLAN-user-personalization.md](PLAN-user-personalization.md) | Identity pipeline (Phase 3 → Stage A) |
| [PLAN-votebot-polis-jigsaw.md](PLAN-votebot-polis-jigsaw.md) | Original monolithic plan (retained as reference) |
