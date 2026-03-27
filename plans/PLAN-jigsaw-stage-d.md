# Jigsaw Stage D: Clustering + Reporting

**Parent plan:** [PLAN-jigsaw-overview.md](PLAN-jigsaw-overview.md)
**Status:** Blocked on Stage C prerequisites (50+ confirmed users).

---

## Goal

Connect to Polis, run clustering over combined chat and direct-vote opinion vectors, and display results on the DDP website and in VoteBot responses. This is the "here's what people think" output — the first time users see collective opinion data derived from their conversations.

---

## Prerequisites

Stage C must have produced **confirmed opinions from 50+ users on at least one bill** before any clustering work begins. Specifically:

- 50+ users have created accounts (Memberstack `member_id`)
- Those users have reviewed and confirmed extracted stances via the consent flow
- At least one bill has 50+ confirmed users with 7+ confirmed positions each (the minimum for Polis "in-conv" inclusion)
- Voter verification pipeline is operational (Catalist DWID matching)

---

## 1. Polis Integration

### Conversation Creation

A Polis conversation is created per bill when the opinion landscape is generated:

```
POST /api/v3/conversations
{
  "topic": "HB 123 — Education Reform",
  "is_anon": false,
  "is_active": true,
  "vis_type": 1
}
```

Each `PolicyPosition` in the landscape becomes a Polis seed statement:

```
POST /api/v3/comments
{
  "conversation_id": "<polis_conversation_id>",
  "body": "Fund through the bill's proposed state formula",
  "is_seed": true
}
```

### XID JWT Generation

VoteBot users are identified to Polis via external IDs (XIDs) encoded in JWTs:

| Field | Value |
|-------|-------|
| XID | `votebot_{member_id}` |
| Algorithm | RS256 |
| Signing key | Polis API key (RSA private key) |
| Expiry | 24 hours |

The `votebot_` prefix ensures no collision with direct Polis participants. JWT is generated server-side and never exposed to the client.

### Vote Discretization

Chat opinion vectors (continuous -1.0 to +1.0) are discretized to Polis votes:

| Continuous stance | Polis vote | Meaning |
|-------------------|------------|---------|
| > +0.3 | +1 (agree) | User supports this position |
| < -0.3 | -1 (disagree) | User opposes this position |
| -0.3 to +0.3 | 0 (pass) | Ambiguous or neutral |

Thresholds at +/- 0.3 avoid false signal from weak or hedged statements.

### Weight Encoding

The `weight_x_32767` field encodes source confidence. Polis stores weights as SMALLINT; divide by 32767 to get the float.

| Source | Weight | `weight_x_32767` |
|--------|--------|-------------------|
| Polis direct vote | 1.0 | 32767 |
| Chat: confirmed via consent | 0.85 | 27851 |
| Chat: explicit selection during elicitation | 0.85 | 27851 |
| Chat: contextual prompt response | 0.7 | 22936 |
| Chat: passive inference (if included) | 0.5 | 16383 |

See `server/src/routes/votes.ts` (line 67) for the conversion: `Math.trunc(weight * 32767)`.

### Redis Mappings

```
votebot:polis:bill:{bill_webflow_id}        → polis_conversation_id
votebot:polis:positions:{bill_webflow_id}    → JSON { position_id: polis_tid, ... }
```

These are set once on conversation creation and updated when positions are added to the landscape.

---

## 2. Matrix Handling (Critical for Valid PCA)

The vote matrix is sparse — chat users typically cover 3-5 of 30+ positions. Incorrect handling of missing values will corrupt PCA results.

### Rules

1. **Missing values = masked, not zero-imputed.** A missing vote means "didn't express an opinion," not "neutral." Polis `named_matrix` already handles this correctly via its sparse representation (`math/src/polismath/math/named_matrix.clj`).

2. **Per-position mean-centering over non-missing entries only.** The column mean for centering is computed only from participants who actually voted on that position. This prevents sparse chat users from pulling means toward zero.

3. **Weights applied as vote multipliers before matrix construction.** A confirmed chat vote of +1 at weight 0.85 enters the matrix as +0.85. This happens in the Polis math service via the existing `weight_x_32767` field — no code changes needed.

4. **Dense Polis voters anchor cluster structure; sparse chat users are placed within it.** Direct Polis voters with full coverage define the principal components. Chat users with partial coverage are projected into the same space. This is the default Polis behavior for participants with fewer than 7 votes (they appear on the map but don't define cluster boundaries).

---

## 3. Vector Confidence Score (Routing Heuristic)

Not all opinion vectors are equal. The vector confidence score determines how a user's data is used in reporting.

### Composite Formula

```
vector_confidence = 0.4 * coverage + 0.3 * avg_confidence + 0.3 * explicit_frac
```

| Component | Definition | Range |
|-----------|-----------|-------|
| `coverage` | Fraction of landscape positions with a non-missing stance | 0.0 - 1.0 |
| `avg_confidence` | Mean extraction confidence across non-missing positions | 0.0 - 1.0 |
| `explicit_frac` | Fraction of stances from explicit elicitation or confirmation (not passive) | 0.0 - 1.0 |

### Routing Thresholds

| Score | Treatment |
|-------|-----------|
| < 0.2 | **Excluded entirely.** Not enough signal to include in any aggregate. |
| 0.2 - 0.5 | **Per-position aggregates only.** Contributes to "X% of people agree with this position" counts but excluded from PCA/clustering. |
| > 0.5 | **Full PCA/clustering.** Included in Polis vote matrix and cluster assignment. |

### Storage

All three components are stored separately in the `opinion_vectors` table:

```sql
ALTER TABLE opinion_vectors ADD COLUMN coverage REAL;
ALTER TABLE opinion_vectors ADD COLUMN avg_confidence REAL;
ALTER TABLE opinion_vectors ADD COLUMN explicit_frac REAL;
ALTER TABLE opinion_vectors ADD COLUMN vector_confidence REAL GENERATED ALWAYS AS (
  0.4 * coverage + 0.3 * avg_confidence + 0.3 * explicit_frac
) STORED;
```

Never hide failure modes behind the composite number. If a user has high coverage but low confidence, that's a different problem than low coverage with high confidence. Debug with the components, route with the composite.

---

## 4. Landscape Snapshot Isolation

Each clustering run is frozen to a specific landscape version to ensure reproducibility. If positions are added or removed between runs, previous cluster results remain interpretable.

### PostgreSQL Table: `clustering_snapshots`

```sql
CREATE TABLE clustering_snapshots (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  bill_webflow_id     TEXT NOT NULL,
  landscape_version   INTEGER NOT NULL,
  positions_included  JSONB NOT NULL,   -- [{position_id, polis_tid, label}, ...]
  participant_count   INTEGER NOT NULL,
  cluster_count       INTEGER NOT NULL,
  results_summary     JSONB NOT NULL,   -- {clusters: [{id, size, top_positions, narrative}], pca_variance: [...]}
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_clustering_snapshots_bill ON clustering_snapshots (bill_webflow_id, created_at DESC);
```

The `positions_included` array records which positions were active at clustering time. The `results_summary` stores enough to render a summary without hitting Polis or Delphi.

---

## 5. Clustering Pipeline

The clustering pipeline leverages the existing Polis math service. **No modifications to the Polis Clojure code are needed.**

See [PLAN-polis-math-pipeline.md](PLAN-polis-math-pipeline.md) for the full data flow.

### Pipeline Steps

| Step | System | What Happens |
|------|--------|-------------|
| 1. Collect votes | VoteBot API | Fetch confirmed chat vectors, discretize, submit to Polis via `/api/v3/votes` |
| 2. Freeze snapshot | VoteBot API | Record landscape version, positions, participant count in `clustering_snapshots` |
| 3. PCA | Polis math (Clojure) | Power iteration finds top 2 principal components from vote matrix |
| 4. K-means | Polis math (Clojure) | Two-level k-means assigns participants to clusters |
| 5. Enrichment | Delphi | UMAP projection, LLM-generated topic labels, narrative synthesis per cluster |
| 6. Cache | VoteBot API | Results written to Redis with 1h TTL |

### Polis Math Details

- **PCA:** Power iteration (not SVD), 100 iterations, top 2 components. See `math/src/polismath/math/pca.clj`.
- **K-means:** Two-level — first pass finds coarse groups, second pass refines. See `math/src/polismath/math/conversation.clj`.
- **Polling:** Math service polls PostgreSQL every 1 second for new votes (`math/src/polismath/poller.clj`). Results available within seconds of vote submission.

### Delphi Enrichment

Delphi adds human-readable meaning to raw cluster data:

- **UMAP projection** for visualization (better separation than PCA alone for display)
- **LLM topic labels** per cluster (e.g., "Funding reformers who support enforcement")
- **Narrative synthesis** per cluster (2-3 sentence summary of what this group believes)
- Results stored in DynamoDB, keyed by `{bill_webflow_id}:{snapshot_id}`

---

## 6. Clustering Results in VoteBot

### Data Sources

VoteBot reads clustering results from:

1. **Polis `math_main`** — cluster assignments, PCA coordinates, group sizes
2. **Delphi DynamoDB** — topic labels, narratives, UMAP projections
3. **Redis cache** — `votebot:clusters:{bill_webflow_id}`, 1h TTL

On cache miss, VoteBot fetches from Polis + Delphi and repopulates the cache.

### Participant Thresholds

| Participants | VoteBot Behavior |
|-------------|-----------------|
| < 10 | "Not enough people have shared their views on this bill yet. You're one of the first!" |
| 10 - 29 | Per-position aggregates only: "Of the 23 people who've weighed in, 74% support the state funding formula." |
| 30 - 99 | Full clusters with margin of error: "Three main groups have emerged among the 67 people who've shared detailed views..." |
| 100+ | Confident reporting: "312 people have weighed in on this bill. Here's what we're seeing..." |

### Example VoteBot Responses

**"What do other people think about this bill?"** (67 participants, 3 clusters)

> Among the 67 people who shared detailed views on HB 123, three main groups have emerged:
>
> **Group 1 (28 people):** Support the state funding formula but want stronger enforcement timelines. They tend to agree that local property tax funding creates inequity.
>
> **Group 2 (22 people):** Prefer local control of funding, with the state providing minimum standards only. They're skeptical of the bill's enforcement mechanisms.
>
> **Group 3 (17 people):** Want to reduce overall education spending and oppose new funding formulas of any kind.
>
> These groups aren't rigid — many people share views across groups. Want to see where your views fit?

**"Where do I stand?"** (user has confirmed opinions, cluster assigned)

> Based on your confirmed views, you're closest to Group 1 — the people who support state funding but want stronger enforcement. You share their support for the funding formula (+0.8) and opposition to property-tax-based funding (-0.6).
>
> Where you differ: you're more supportive of the 5-year timeline than most people in your group. On that point, you're actually closer to Group 2.

**"Where do I stand?"** (user has opinions but < 7 confirmed positions)

> I have a few of your views on this bill, but not enough to place you on the opinion map yet. You've confirmed stances on 4 of the 12 key positions. Want to go through a few more so I can show you where you stand?

---

## 7. Opinion Report Pages on DDP Website

### Per-Bill Report

Each tracked bill with 30+ cluster participants gets a public opinion report page:

- **Cluster visualization** — UMAP scatter plot with labeled groups
- **Group summaries** — Delphi-generated narratives per cluster
- **Position-level breakdown** — bar charts showing agree/disagree/pass per position
- **Sample size badge** — prominently displayed: "Based on 312 discussions, 187 confirmed views"
- **Last updated timestamp** — when clustering last ran

### Per-District Report

For bills with sufficient verified voters in a district:

- **District participation count** — "89 verified voters in District 14 have weighed in"
- **District-specific cluster distribution** — how this district's voters are distributed across the bill-wide clusters
- **Comparison to statewide** — "District 14 skews toward Group 1 (42% vs. 33% statewide)"
- **Minimum threshold:** 15 verified voters per district before showing district-specific data

### Methodology Page

A standing methodology page linked from every report:

- How opinions are collected (chat extraction + direct voting)
- How confidence weighting works
- Sample size thresholds and what they mean
- How clusters are computed (PCA + k-means, non-technical explanation)
- What "verified voter" means (Catalist matching)
- Limitations: self-selected sample, not a poll, not representative of all voters
- Confidence intervals where applicable

---

## 8. Participation Level Language (Product Honesty)

Every user-facing number must be labeled with its actual meaning. Never conflate aggregate discussion counts with cluster inclusion counts.

| Context | Language Template | What It Means |
|---------|-----------------|---------------|
| Aggregate stats | "Based on [N] discussions about this bill..." | Anyone who chatted about the bill and expressed at least one opinion-like statement |
| Cluster results | "Among the [N] people who shared detailed views..." | Users with 7+ confirmed positions, included in PCA/clustering |
| District data | "Of the [N] verified voters in your district..." | Cluster participants with Catalist DWID match in that district |
| Early stage | "[N] people have started sharing their views..." | Fewer than threshold, encouraging participation |

### Anti-Patterns (Do Not Do This)

- "312 people agree that..." (conflates discussion with agreement)
- "Most people think..." (unquantified, implies representativeness)
- "Voters in your district support..." (without specifying sample size or verification level)

---

## 9. Weight Calibration

Run after 200+ opinions have been collected to validate passive extraction accuracy.

### Calibration Process

1. Identify users who have both passive extractions and confirmed stances for the same position
2. Compare passive stance direction (agree/disagree) against confirmed direction
3. Compute agreement rate per elicitation mode

### Decision Thresholds

| Passive Agreement Rate | Action |
|------------------------|--------|
| > 80% | Weight is too conservative. Consider raising passive weight from 0.5 to 0.6. |
| 60% - 80% | Weight is appropriate. No change needed. |
| < 60% | Weight is too generous. Lower passive weight to 0.3, or exclude passive extractions from aggregates entirely. |

### Ongoing Monitoring

- Track correction rate per elicitation mode (passive, contextual, explicit) on a rolling 30-day basis
- Alert if any mode's agreement rate drops below 60%
- Store calibration results in `weight_calibration_log` for audit

```sql
CREATE TABLE weight_calibration_log (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_date        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  mode            TEXT NOT NULL,         -- 'passive', 'contextual', 'explicit'
  sample_size     INTEGER NOT NULL,
  agreement_rate  REAL NOT NULL,
  previous_weight REAL NOT NULL,
  new_weight      REAL,                  -- NULL if no change
  notes           TEXT
);
```

---

## 10. System of Record

| Data | System of Record | Cache |
|------|-----------------|-------|
| Policy positions (landscape) | PostgreSQL | Redis (votebot:landscape:{id}) |
| Polis votes | Polis PostgreSQL (`votes`, `votes_latest_unique`) | — |
| Opinion vectors | PostgreSQL (`opinion_vectors`) | Redis (votebot:vectors:{member_id}) |
| Clustering results | Polis `math_main` + Delphi DynamoDB | Redis (votebot:clusters:{id}, 1h TTL) |
| Clustering snapshots | PostgreSQL (`clustering_snapshots`) | — |
| User accounts | Memberstack + PostgreSQL | Redis (session cache) |

---

## 11. Distributed System Failure Handling

| Component Down | Behavior | Recovery |
|---------------|----------|----------|
| **Redis** | Degrade personalization. Skip cache reads/writes. Serve clustering results directly from Polis + Delphi (slower). | Auto-recover on reconnect. Cache repopulates on next read. |
| **PostgreSQL** | Queue opinion vector writes in Redis (`votebot:pg_queue`). Serve cached data. No new snapshots created. | Drain Redis queue to PostgreSQL on recovery. |
| **Polis** | Queue votes in Redis (`votebot:polis_vote_queue`). Return cached cluster results. New votes are not reflected until recovery. | Replay queued votes to Polis on recovery. Trigger recomputation. |
| **Delphi** | Use raw Polis math data. Cluster assignments and PCA coordinates still available. No narrative labels — show position lists instead. | Re-run Delphi enrichment on recovery. Update cache. |

All queued operations include timestamps and are idempotent (Polis votes upsert, vector writes upsert).

---

## 12. Recomputation Frequency

| Trigger | When |
|---------|------|
| **Scheduled** | Daily at 05:00 UTC |
| **Threshold** | 20+ new confirmed opinions since last run |
| **On-demand** | Admin trigger via internal API (`POST /admin/clustering/recompute/{bill_webflow_id}`) |

The scheduled run processes all bills with new data. Threshold-triggered runs process only the affected bill. On-demand runs are rate-limited to 1 per bill per hour.

---

## 13. Validation Gates

All three gates must pass before clustering results are shown publicly.

### Gate 1: Human Intuition Match

- 3 reviewers independently examine cluster results for a bill
- Each reviewer rates whether the cluster groupings and narratives match their reading of the underlying opinions
- Threshold: > 70% agreement across reviewers
- Performed on the first 3 bills to reach 30+ participants, then spot-checked quarterly

### Gate 2: Cluster Stability

- Remove a random 10% of participants and re-run clustering
- Compare cluster assignments for the remaining 90%
- Threshold: > 85% of participants assigned to the same cluster (or a cluster with > 80% overlap)
- Automated — runs as part of every clustering pipeline execution

### Gate 3: Sample Size Enforcement

- Participant thresholds (< 10, 10-29, 30-99, 100+) are enforced in code, not just UI
- VoteBot API returns the appropriate response tier based on participant count
- District reports require 15+ verified voters — no exceptions, no "close enough"

---

## 14. PostgreSQL Tables Introduced

This stage introduces:

| Table | Purpose |
|-------|---------|
| `clustering_snapshots` | Frozen snapshot per clustering run (see Section 4) |
| `weight_calibration_log` | Audit trail for weight adjustments (see Section 9) |

Schema additions to existing tables:

| Table | Columns Added |
|-------|--------------|
| `opinion_vectors` | `coverage`, `avg_confidence`, `explicit_frac`, `vector_confidence` (generated) |

---

## 15. What This Stage Does NOT Include

- **Emergent positions** — detecting new policy positions from user conversations (Stage E)
- **Polis embed on DDP site** — direct Polis voting widget alongside VoteBot chat (Stage E)
- **Legislative drafting** — translating cluster consensus into policy language (future, unspecified)
- **Real-time clustering** — clusters update on a schedule, not per-vote
- **Cross-bill analysis** — comparing opinion patterns across related bills
