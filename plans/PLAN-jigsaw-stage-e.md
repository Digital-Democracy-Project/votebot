# Jigsaw Stage E: Emergent Positions + Full Loop + Future

> Part of [Jigsaw Rollout Plan](./PLAN-jigsaw-overview.md)
>
> **Status:** Blocked on Stage D operational clustering.

---

## 1. Goal

The system learns from users and improves itself. Stage E closes the loop:

- **Emergent positions** expand the opinion landscape based on novel claims users actually raise.
- **Polis embed** on DDP bill pages gives visitors a direct voting interface alongside VoteBot chat.
- **Bidirectional flow** between chat and Polis ensures both modalities enrich the same opinion space.
- **Conceptual roadmap** for legislative drafting establishes the long-term direction without committing to implementation.

---

## 2. Prerequisites (Stage D Validation Gates)

| Gate | Threshold |
|------|-----------|
| Clustering operational | Polis clusters producing stable, interpretable groups |
| Cluster quality (human review) | >= 3.5 / 5 on meaningfulness |
| Opinion maps live on DDP | At least 3 bills with published opinion reports |

---

## 3. v1 Posture: Conservative

Bias toward **fewer emergent additions** and **stricter approval**. A growing position space improves coverage but reduces interpretability — every new dimension makes clustering harder and opinion vectors sparser.

> **The deep system risk:** Stage E is where the system stops just measuring opinion and starts **shaping the space of possible opinions**. Users express views → system creates positions → future users select from them → those selections reinforce positions. This feedback loop is unavoidable but must be monitored and managed. Left unchecked, the system can converge on its own framing of reality.

| Principle | Rationale |
|-----------|-----------|
| High threshold for new positions | Avoid noise from one-off rants |
| Auto-approve + notify (not auto-approve silently) | Admin awareness on every addition |
| No removal of initial positions | Bill-text-anchored positions are stable ground truth |
| Expand automation only after observing real patterns | Let the data teach us what "good enough" looks like |
| **Max 3-5 new emergent positions per bill per week** (soft cap) | Prevents runaway expansion; admin can override |
| **Diversity constraint on emergence** | Claims must come from different sessions + different time windows |
| **Track emergent vs original balance** | If emergent positions dominate selections, the system is drifting |

---

## 4. Emergent Position Pipeline

```
Novel claims (Stage B)
    │
    ▼
Accumulation buffer (per bill)
    │
    ▼
Threshold check: >= 5 distinct users?
    │  no → wait
    ▼  yes
Cluster with SentenceTransformer + HDBSCAN
    │
    ▼
Cluster size >= 3 claims?
    │  no → discard cluster
    ▼  yes
LLM generates candidate PolicyPosition
    │
    ▼
Quality gate (3 checks)
    │  fail → discard with log
    ▼  pass
Integration: add to landscape, seed Polis, backfill vectors
    │
    ▼
Admin notification
```

### 4.1 Accumulation

- [ ] Novel claims flagged during Stage B extraction accumulate in a per-bill buffer
- [ ] Each claim stored with: `visitor_id`, `bill_id`, `raw_text`, `embedding`, `timestamp`
- [ ] Deduplication: skip claims with cosine similarity > 0.92 to an existing buffered claim from the same visitor

### 4.2 Threshold + Diversity Constraint

- [ ] Minimum **5 distinct users** with novel claims on the same bill before clustering runs
- [ ] **Diversity constraint:** the 5 users must come from at least **3 distinct sessions** (not all from one session) AND span at least **48 hours** of calendar time. This prevents a small coordinated group from injecting positions in a single burst.
- [ ] Check runs on a schedule (daily) or triggered when a new novel claim arrives and count crosses threshold
- [ ] **Weekly cap:** max 3-5 new emergent positions per bill per week (soft cap, admin override available)

### 4.3 Clustering

- [ ] Encode all buffered novel claims with SentenceTransformer (same model as landscape embeddings)
- [ ] Cluster with HDBSCAN (`min_cluster_size=3`, `min_samples=2`)
- [ ] Each resulting cluster of 3+ claims becomes a candidate for position generation

### 4.4 Candidate Generation

- [ ] For each qualifying cluster, pass the raw claim texts to the LLM
- [ ] Prompt: "Generate a single PolicyPosition statement that captures this cluster of user opinions. Follow the style of existing positions: concise, specific to the bill, neutral framing."
- [ ] LLM returns: `position_text`, `topic` (existing or new), `bill_sections` (if identifiable)

### 4.5 Quality Gate

| Check | Criterion | Failure action |
|-------|-----------|----------------|
| Semantic distance | Cosine distance from nearest existing position > **adaptive threshold** (0.20 when landscape < 20 positions, 0.15 when 20-40, 0.12 when 40+). Stricter when landscape is small to prevent early duplicates; relaxes as the space fills. | Reject (too similar to existing) |
| Specificity | Position references concrete policy mechanism, not vague sentiment | Reject with reason logged |
| Bill relevance | Position relates to provisions in the bill text | Reject with reason logged |

### 4.6 Integration

- [ ] Add approved position to landscape with `source: "emergent"` and `created_at` timestamp
- [ ] **Position lineage tracking:** every emergent position records its origin metadata:
  ```json
  {
    "source": "emergent",
    "origin_cluster_size": 4,
    "origin_claim_count": 7,
    "origin_user_count": 5,
    "origin_time_span_hours": 72,
    "first_seen": "2026-05-01T...",
    "adoption_rate": 0.0,
    "growth_rate": 0.0
  }
  ```
- [ ] Generate corresponding Polis seed statement and submit via Polis API
- [ ] Backfill existing opinion vectors: set new dimension to `null` (unasked) for all prior users
- [ ] Admin notification on every new emergent position (email + dashboard alert)

---

## 5. Why Bill-Scoped Topics Minimize Drift

A common concern with emergent positions: will the landscape drift away from the bill over time?

Bill-scoped anchoring provides natural guardrails:

| Factor | Effect |
|--------|--------|
| Initial positions are extracted from bill text | Anchored to concrete statutory provisions |
| Emergent positions must pass bill-relevance check | Cannot introduce off-topic dimensions |
| Bill text does not change (unless formally amended) | The anchor is stable |
| Emergent positions expand but do not move existing axes | Original positions remain fixed coordinates |
| Amendment tracking (if implemented) | Triggers landscape refresh from new text, not drift |

Drift risk is proportional to how loosely "bill relevance" is defined. The v1 posture (conservative, admin-notified) keeps this tight.

---

## 6. Polis Embed on DDP Bill Pages

### Architecture

```
┌──────────────────────────────────────────┐
│         DDP Bill Page (Webflow)          │
│                                          │
│  ┌─────────────────┐  ┌──────────────┐  │
│  │  VoteBot Chat    │  │ Polis Embed  │  │
│  │  Widget          │  │ (iframe)     │  │
│  │                  │  │              │  │
│  │  Guided          │  │ Standard     │  │
│  │  elicitation     │  │ agree/       │  │
│  │  (Stages A-B)    │  │ disagree/    │  │
│  │                  │  │ pass voting  │  │
│  └─────────────────┘  └──────────────┘  │
└──────────────────────────────────────────┘
```

### Behavior

- [ ] Polis statements = policy positions from the bill's landscape
- [ ] Users can vote directly in the Polis embed (standard Polis experience)

### Population Mixing Awareness

Chat users and Polis direct voters are **different populations** with different behavioral patterns:

| Population | Behavior | Vector Density | Risk |
|---|---|---|---|
| Chat users | Exploratory, conversational | Sparse (3-10 positions) | Underrepresented in clustering |
| Polis voters | Decisive, structured | Dense (most/all positions) | May dominate cluster structure |

This mixing is by design — it's what makes the system more comprehensive than either modality alone. But it means clusters are shaped more by dense Polis voters than sparse chat users. The methodology must acknowledge this:

- [ ] Track and report the source mix in every clustering run (% chat-origin vs. % Polis-origin participants)
- [ ] If Polis voters are >80% of cluster participants, note this in public reporting: "Clusters primarily reflect structured voting; chat-derived opinions are supplementary"
- [ ] Monitor whether adding the Polis embed changes cluster structure materially vs. chat-only (run both and compare for the first 3 bills)
- [ ] If a user has also chatted and confirmed opinions, pre-vote those positions via Polis XID mapping
- [ ] Embed alongside VoteBot chat widget on each bill page
- [ ] Polis conversation ID stored in bill metadata (`polis_conversation_id`)

### XID Mapping

| Scenario | Polis behavior |
|----------|---------------|
| User chatted + confirmed stances, then opens Polis | Pre-voted statements shown as already voted |
| User votes in Polis first, then chats | Chat is aware of existing Polis votes; skips re-asking |
| User has no account (anonymous Polis voter) | Standard Polis flow; no cross-modality linking |

---

## 7. Bidirectional Flow

```
         Chat                              Polis
         ────                              ─────
  Novel claims detected ──────────►  New seed statements
                                     added to conversation

  Cluster summaries     ◄──────────  Voting reveals cluster
  used in responses                  structure (PCA groups)

  Sparse vectors        ◄──────────  Polis voters cover
  get filled                         positions chat missed

  Landscape expanded    ──────────►  New positions become
  with emergent claims               new Polis statements
```

| Direction | What flows | Effect |
|-----------|-----------|--------|
| Chat → Polis | New positions from emergent pipeline | Polis statements stay current with real concerns |
| Polis → Chat | Cluster structure and vote distributions | VoteBot can summarize "what people think" |
| Polis → Vectors | Votes on positions chat users skipped | Fills sparse opinion vectors |
| Chat → Landscape | Novel claims expand position space | Coverage grows with user diversity |

---

## 8. VoteBot Awareness of Clusters

Every bill conversation can draw on live cluster data to enrich the dialog. Three modes:

### 8.1 Direct Cluster Summary

**Trigger:** User asks "What do people think about this?" or similar.

> **User:** What do other people think about this bill?
>
> **Bot:** Based on 847 participants, there are three main groups:
>
> - **Group A (42%):** Support the bill's density provisions but want stronger infrastructure requirements
> - **Group B (31%):** Oppose statewide mandates, prefer local control
> - **Group C (22%):** Support the bill as-is, including the transit funding provisions
>
> _Representative quote from Group A: "I'm fine with fourplexes, but not without a plan for schools and water."_

### 8.2 Alignment Feedback

**Trigger:** After user expresses an opinion on a position.

> **Bot:** Your view aligns with 42% of participants -- they generally support density but share your concern about infrastructure. They also tend to support the transit funding provision in Section 4. What's your take on that?

### 8.3 Guided Elicitation Enrichment

**Trigger:** During active elicitation, use cluster correlations to surface likely-relevant positions.

> **Bot:** Most people who support limiting agricultural consolidation also support the small-farm grant program in Section 7 -- what about you?

### 8.4 Counterfactual Elicitation (anti-reinforcement)

**Critical safeguard against self-reinforcing clusters.** If VoteBot only says "people who support X also support Y," it guides users toward cluster-consistent answers, making clusters more defined than reality.

**Counterfactual prompts** deliberately probe across cluster boundaries:

> **Bot:** Interestingly, some people who support the funding formula actually oppose the enforcement timeline — they think the formula is right but implementation is too rushed. What's your view on the timeline?

- [ ] For every 2-3 correlation-based prompts (8.3), include at least 1 counterfactual prompt
- [ ] Counterfactual targets: positions where intra-cluster agreement is lowest (most internal disagreement)
- [ ] Frame as genuine diversity, not as a challenge: "Interestingly..." / "Some people who agree with you on X actually differ on Y..."

This prevents clusters from becoming self-fulfilling prophecies.

### Implementation Tasks

- [ ] Implement cluster summary endpoint (returns top N groups with % and representative quotes)
- [ ] Implement alignment lookup (given a partial opinion vector, return nearest cluster)
- [ ] Implement correlation-based suggestion (given confirmed stances, return positions with highest intra-cluster correlation that user hasn't addressed)
- [ ] Implement counterfactual suggestion (return positions with lowest intra-cluster agreement for the user's current cluster)
- [ ] Rate-limit cluster references: max 2 per session to avoid making users feel categorized

---

## 9. Unified Event Model

All opinion-related events across analytics and the opinion system write to a single JSONL event log for a unified audit trail.

### Event Types

| Event | Trigger | Key fields |
|-------|---------|------------|
| `opinion_extracted` | Extraction runs on a message | `visitor_id`, `bill_id`, `position_id`, `stance`, `confidence`, `source_message_id` |
| `opinion_confirmed` | User confirms in consent flow | `visitor_id`, `bill_id`, `position_id`, `stance`, `consent_type` |
| `opinion_submitted` | Stances submitted to Polis | `visitor_id`, `polis_xid`, `bill_id`, `position_ids[]`, `polis_conversation_id` |
| `account_created` | Memberstack account created | `visitor_id`, `member_id`, `timestamp` |
| `voter_verified` | Catalist verification succeeds | `member_id`, `voter_id`, `jurisdiction`, `timestamp` |

### Event Schema (common envelope)

```json
{
  "event_type": "opinion_confirmed",
  "timestamp": "2026-03-27T14:22:01Z",
  "visitor_id": "v_abc123",
  "session_id": "s_def456",
  "payload": { ... }
}
```

- [ ] Define JSONL event log location and rotation policy
- [ ] Implement event emitters for each event type
- [ ] Ensure all five event types share the common envelope schema
- [ ] Add log ingestion for downstream analytics (e.g., aggregate queries, dashboards)
- [ ] Verify consent and provenance fields are present on every opinion event

---

## 10. Phase 8: Future — Legislative Drafting Pipeline

> **Strictly internal for now.** Even though this section is labeled "conceptual," users and stakeholders will hear "this system can generate policy" if it's discussed externally. Keep this roadmap internal until the opinion system is validated at scale and drafting tools are evaluated. Premature expectations create credibility pressure that can compromise the opinion system's integrity.

**This phase is conceptual only.** No implementation is specified or scheduled.

### Conceptual Flow

```
Consensus positions (from clustering)
    │
    ▼
Policy directives (plain-language goals)
    │
    ▼
Legislative text (statutory amendments or new sections)
    │
    ▼
Community review (verified voters review and iterate)
```

### Why the Current Data Model Supports It

| Existing element | How it supports drafting |
|-----------------|------------------------|
| `PolicyPosition.bill_sections` | Maps opinions to specific statutory provisions |
| `novel_claims` | Captures concerns not in original bill text |
| PostgreSQL relational model | Supports complex queries across positions, votes, and verification |
| `verified_voters` + `opinion_vectors` JOIN | Enables district-level queries ("What do verified voters in District 5 think?") |
| Consent + provenance trail | Every opinion traceable to source, with explicit user consent |

### What's Needed (Not Yet Built)

- Legislative drafting LLM service (specialized for statutory language)
- Statutory code retrieval (current law for the jurisdiction)
- Amendment formatting conventions (per-jurisdiction style requirements)
- Legal review workflow (human-in-the-loop before any public output)

### Open Questions

- What existing tools support AI-assisted legislative drafting?
- Full amendment text vs. policy brief -- which output is more useful?
- Legal and constitutional constraints on AI-generated legislative language?
- Minimum participant count for democratic legitimacy -- what threshold justifies "the people want X"?

---

## 11. Tasks

### Emergent Position Pipeline
- [ ] Novel claim clustering pipeline (SentenceTransformer + HDBSCAN)
- [ ] Candidate position generation via LLM
- [ ] Quality gate: semantic distance, specificity, bill-relevance checks
- [ ] Auto-injection into landscape with `source: "emergent"` tag
- [ ] Auto-injection into Polis as seed statement
- [ ] Admin notification on every new emergent position
- [ ] Backfill existing opinion vectors with new dimension

### Polis Embed
- [ ] Polis embed integration on DDP bill pages
- [ ] XID mapping for cross-modality pre-voting
- [ ] Polis conversation ID stored in bill metadata

### Cross-Modality Flow (Chat <-> Polis)
- [ ] Chat-discovered positions flow to Polis as seed statements
- [ ] Polis cluster structure available to VoteBot responses
- [ ] Sparse vector backfill from Polis votes
- [ ] Emergent landscape expansion reflected in both modalities

### VoteBot Cluster Awareness
- [ ] Cluster summary endpoint
- [ ] Alignment feedback after opinion expression
- [ ] Correlation-based guided elicitation suggestions
- [ ] **Counterfactual elicitation prompts** (1 per 2-3 correlation prompts)
- [ ] Rate limiting on cluster references (max 2/session)

### Unified Event Model
- [ ] JSONL event log with common envelope schema
- [ ] Event emitters for all five event types
- [ ] Log ingestion pipeline for analytics

### Emergence Monitoring Dashboard (non-optional)

This is the primary safeguard against feedback loops and runaway drift. Must be built alongside the emergent pipeline, not after.

- [ ] **Position lineage view**: for every emergent position, show origin claim count, origin user count, time span, adoption rate, growth rate
- [ ] **Emergent vs original balance**: % of user selections from emergent positions vs. bill-text-anchored positions. Alert if emergent selections exceed 40% — system may be drifting from bill-anchored reality.
- [ ] **Coverage decay monitor**: average user coverage (positions filled / total positions) over time. If coverage is declining, the landscape is growing faster than users can keep up — slow down emergence.
- [ ] **Adoption rate per position**: how quickly each emergent position accumulates stances after creation. Fast adoption = real demand. Slow adoption = possibly noise.
- [ ] **Coordinated noise detection**: flag emergent positions where origin claims cluster in a narrow time window from few IPs/visitors
- [ ] **Position count per bill over time**: chart showing landscape growth. If a bill reaches 60+ positions, flag for consolidation review.

### Production Operations
- [ ] Production monitoring for emergent pipeline (latency, rejection rate, quality scores)
- [ ] A/B testing framework for cluster-aware responses vs. standard
- [ ] Iteration plan: review emergent positions weekly, adjust thresholds based on data
- [ ] **Weekly admin review** of emergence dashboard: new positions, adoption rates, coverage trends, balance metrics

---

## 12. What This Stage Does NOT Include

- **Actual legislative drafting implementation** -- Section 10 is a conceptual roadmap only
- Legislative text generation or amendment formatting
- Legal review tooling
- Public-facing "draft legislation" features

These are deferred to a future phase, pending validation of the opinion system at scale and evaluation of emerging AI-assisted drafting tools.
