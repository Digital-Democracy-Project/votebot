# How Opinion Vectors Flow Through the Polis Math Pipeline

This document traces the exact data flow from when a user's opinion enters Polis as votes through PCA, clustering, and representativeness analysis to reportable results. All code references are to the polis repo.

> **Context:** This is the technical reference for the clustering pipeline described in `plans/PLAN-votebot-polis-jigsaw.md` Phase 4. The Jigsaw plan defines the opinion landscape (topics, policy positions), elicitation flow, and identity model. This document covers what happens to those opinions once they enter Polis as votes — the math is identity-agnostic and requires no modifications to support VoteBot chat input.

---

## Step 1: Opinions Enter Polis as Votes

Each policy position in the opinion landscape IS a Polis seed comment (`is_seed=true`). When a user's opinions are submitted — via the consent flow in VoteBot chat or direct voting in the Polis embed — they become rows in the `votes` table.

**Example: Chat user says "I support the state formula but think enforcement is too weak"**

Discretized from the continuous topic-stance vector:
- P1 (funding_formula): +1 (stance was +0.7, above +0.3 threshold)
- P8 (3yr_timeline): -1 (stance was -0.8, below -0.3 threshold)
- P9 (5yr_timeline): +1 (stance was +0.7, above +0.3 threshold)

Three `POST /api/v3/votes` calls, one per position.

**PostgreSQL writes:**

Two tables are updated per vote:
- `votes` — full history, append-only
- `votes_latest_unique` — current state, upserted via a PostgreSQL rule

```sql
-- The DB rule that maintains current state:
CREATE RULE on_vote_insert_update_unique_table AS
    ON INSERT TO votes
    DO ALSO
        INSERT INTO votes_latest_unique (zid, pid, tid, vote, weight_x_32767, modified)
        VALUES (NEW.zid, NEW.pid, NEW.tid, NEW.vote, NEW.weight_x_32767, NEW.created)
        ON CONFLICT (zid, pid, tid) DO UPDATE
        SET vote = excluded.vote, modified = excluded.modified;
```

**Resulting rows in `votes_latest_unique`:**

```
zid=123, pid=67, tid=5  (P1: state formula),    vote=+1, weight_x_32767=27851
zid=123, pid=67, tid=12 (P8: 3-year timeline),  vote=-1, weight_x_32767=27851
zid=123, pid=67, tid=13 (P9: 5+ years),         vote=+1, weight_x_32767=27851
```

**Vote weight encodes source confidence:**

The `weight_x_32767` field (SMALLINT, divided by 32767 to get a float in [-1, 1]) is built into Polis. We use it to distinguish input sources:

| Source | Weight | weight_x_32767 |
|--------|--------|-----------------|
| Polis direct vote | 1.0 | 32767 |
| Chat: confirmed via consent | 0.85 | 27851 |
| Chat: explicit selection during elicitation | 0.85 | 27851 |
| Chat: contextual prompt response | 0.7 | 22936 |
| Chat: passive inference (if included) | 0.5 | 16383 |

**Source files:**
- Vote submission handler: `server/src/routes/votes.ts` (lines 201-284)
- Weight conversion: `Math.trunc(weight * 32767)` (line 67)
- DB rule: `server/postgres/migrations/` (votes_latest_unique trigger)

---

## Step 2: Math Service Detects New Votes

The Clojure math service polls PostgreSQL every **1 second** for new votes:

```sql
SELECT * FROM votes
WHERE created > {last_timestamp}
ORDER BY zid, tid, pid, created
```

New votes are grouped by `zid` (conversation ID = bill) and routed to that conversation's actor via the conversation manager. This is fully automatic — no trigger or webhook needed. As soon as votes land in the DB, the math service picks them up within 1 second.

**Source files:**
- Poller: `math/src/polismath/poller.clj` (lines 12-37)
- PostgreSQL query: `math/src/polismath/components/postgres.clj` (lines 132-145)
- Conversation routing: `math/src/polismath/conv_man.clj` (line 34)

---

## Step 3: Vote Matrix Construction

The math service maintains a **named matrix** per conversation. Rows are participants, columns are statements (our policy positions), values are votes.

```
               P1(formula) P2(property) P3(realloc) P4(reduce) ... P9(5yr) P10(immediate)
polis_user_1:    +1          -1            0           -1            0         +1
polis_user_2:    -1          +1           +1            0           -1          0
chat_user_3:     +1           ?            ?            ?           +1          ?
chat_user_4:      ?          +1            ?           -0.7          ?         +1
```

Missing values (`?`) are entries where the participant didn't vote on that position. The matrix is sparse, especially for chat users who may only cover 3-5 of 30 positions. PCA handles this by working with the non-missing entries.

**Matrix construction** (from `conversation.clj`, lines 192-196):

```clojure
:raw-rating-mat
(plmb/fnk [conv keep-votes]
  (nm/update-nmat
    (:raw-rating-mat conv)
    (map (fn [v] (vector (:pid v) (:tid v) (:vote v))) keep-votes)))
```

Each vote is a `(participant_id, statement_id, vote_value)` triple that updates the named matrix.

**Filtering and limits:**
- Maximum 100,000 participants per conversation
- Maximum 10,000 comments/statements per conversation
- Moderated-out comments are zeroed in the matrix
- Participants must have voted on at least 7 statements OR be in the top 15 voters to be included in the "in-conv" set. **Note for VoteBot integration:** Chat users who cover only 3-5 positions will be excluded from clustering. See `PLAN-votebot-polis-jigsaw.md` question 14 for mitigation strategies.

**Source files:**
- Matrix construction: `math/src/polismath/math/conversation.clj` (lines 164-205)
- Named matrix implementation: `math/src/polismath/math/named_matrix.clj`

---

## Step 4: PCA — Finding the Real Axes of Disagreement

PCA reduces the high-dimensional vote matrix (participants x positions) to 2 principal components for visualization and analysis.

### Algorithm: Power Iteration (not SVD)

Polis uses power iteration rather than full SVD, which is efficient for finding the top 2 components of a large sparse matrix.

**Step 4a: Center the data**

Subtract the mean vote per position (column) from the matrix. This ensures PCA finds axes of *variance*, not just overall sentiment.

**Step 4b: Find PC1 — the dominant axis of disagreement**

```clojure
;; Power iteration (pca.clj, lines 38-56):
;; 1. Start with random vector v
;; 2. Repeat 100 times:
;;    v = X^T * X * v    (multiply by covariance matrix)
;;    v = v / ||v||       (normalize)
;; 3. v converges to the first eigenvector
```

PC1 is the direction in opinion space along which people disagree the most. It might represent "pro-bill vs. anti-bill" if that's the dominant split, or something more nuanced like "funding-focused vs. enforcement-focused."

**Step 4c: Find PC2 — the second axis**

Gram-Schmidt orthogonalization removes PC1's influence from the data, then power iteration finds the next direction of maximum variance.

```clojure
;; Gram-Schmidt (pca.clj, lines 66-76):
;; Factor out PC1 from the data matrix
;; Run power iteration again on the residual
;; PC2 is orthogonal to PC1 by construction
```

**Step 4d: Project every participant onto the 2D plane**

```clojure
(defn pca-project [data pca]
  (matrix/mmul (- data center) (matrix/transpose comps)))
```

Each participant gets an (x, y) coordinate. Each statement/position also gets a projected location showing where it "pulls" in opinion space.

### PCA Output

```json
{
  "comps": [
    [0.35, -0.21, 0.18, 0.42, ...],
    [0.12, 0.44, -0.31, 0.08, ...]
  ],
  "center": [0.23, -0.11, 0.05, 0.15, ...],
  "comment-projection": {
    "5": [0.35, 0.12],
    "12": [-0.21, 0.44],
    "13": [0.18, -0.31]
  },
  "comment-extremity": [0.82, 0.91, 0.45, ...]
}
```

- `comps` — the two principal component vectors. Each entry corresponds to a position (tid). High absolute values mean that position strongly loads on that axis.
- `center` — the mean vote per position (subtracted during centering)
- `comment-projection` — where each position falls in the 2D PCA space
- `comment-extremity` — how divisive each position is (higher = more variance = more disagreement)

### What the Axes Mean

The PCA components are linear combinations of policy positions. If PC1 has high positive loading on P1 (state formula) and high negative loading on P4 (reduce spending), then PC1 represents the "more funding vs. less funding" axis. The positions that load most heavily on each PC are the ones that most divide people.

**Source files:**
- Power iteration: `math/src/polismath/math/pca.clj` (lines 38-56)
- Gram-Schmidt: `math/src/polismath/math/pca.clj` (lines 66-76)
- Projection: `math/src/polismath/math/pca.clj` (lines 127-131)
- Full PCA wrapper: `math/src/polismath/math/pca.clj` (lines 108-124)

---

## Step 5: K-Means Clustering — Finding Opinion Groups

K-means runs in the 2D PCA-projected space to group participants with similar opinion patterns.

### Two-Level Clustering

**Level 1: Base clusters (~100 micro-groups)**

Fine-grained clustering to reduce noise. Participants very close together in PCA space are grouped.

**Level 2: Group clusters (2-5 macro-groups)**

Base clusters are themselves clustered into the 2-5 groups that users actually see. This two-level approach is more stable than clustering raw participants directly.

### K Selection

```clojure
;; conversation.clj, lines 275-276:
(min max-max-k (+ 2 (int (/ (count (nm/rownames data)) 12))))
```

- 50 participants → k = min(5, 2 + 4) = 5
- 24 participants → k = min(5, 2 + 2) = 4
- 12 participants → k = min(5, 2 + 1) = 3

### K-Means Algorithm

```clojure
(defn kmeans [data k & {:keys [last-clusters max-iters weights]}]
  (loop [clusters clusters iter max-iters]
    (let [new-clusters (cluster-step data-iter k clusters :weights weights)]
      (if (or (= iter 0) (same-clustering? clusters new-clusters))
        new-clusters
        (recur new-clusters (dec iter))))))
```

Each iteration:
1. Assign each participant to the nearest cluster center
2. Recompute cluster centers as the weighted mean of members
3. Remove empty clusters
4. Repeat until assignments stop changing

### Clustering Output

```json
{
  "group-clusters": [
    {"id": 0, "center": [0.45, -0.32], "members": [0, 3, 7]},
    {"id": 1, "center": [-0.38, 0.21], "members": [1, 2, 5]},
    {"id": 2, "center": [-0.12, -0.55], "members": [4, 6]}
  ],
  "base-clusters": {
    "x": [0.4, -0.3, 0.5, ...],
    "y": [-0.3, 0.2, -0.5, ...],
    "id": [0, 1, 2, ...],
    "count": [45, 38, 22, ...],
    "members": [[pid1, pid2, ...], [pid5, pid6, ...], ...]
  }
}
```

- `group-clusters[].members` are base-cluster IDs (not individual PIDs)
- `base-clusters.members` are individual participant IDs per base cluster
- `center` is the (x, y) position in PCA space

**Source files:**
- K-means: `math/src/polismath/math/clusters.clj` (lines 301-312)
- Cluster step: `math/src/polismath/math/clusters.clj` (lines 142-158)
- K selection: `math/src/polismath/math/conversation.clj` (lines 275-276)

---

## Step 6: Representativeness — What Defines Each Group

For each combination of group and policy position, Polis computes how differently this group votes compared to everyone else. This determines which positions are **most representative** of each group — the statements that define what that group believes.

### Per-Group-Position Statistics

```clojure
;; repness.clj, lines 78-82:
(defn comment-stats [vote-col]
  {:na (count-votes vote-col -1)        ; agree count (note: Polis internal convention)
   :nd (count-votes vote-col 1)         ; disagree count
   :ns (count-votes vote-col)           ; total votes seen
   :pa (/ (+ 1 na) (+ 2 ns))           ; probability of agree (Bayesian smoothing)
   :pd (/ (+ 1 nd) (+ 2 ns))})         ; probability of disagree
```

Bayesian smoothing (Laplace prior: `(count + 1) / (total + 2)`) prevents zero-probability issues with small groups.

### Comparative Statistics

```clojure
;; repness.clj, lines 85-100:
:ra (/ pa_in_group pa_everyone_else)    ; agree ratio
:rd (/ pd_in_group pd_everyone_else)    ; disagree ratio
:rat (two-prop-test na_in ns_in na_rest ns_rest)  ; agree z-score
:rdt (two-prop-test nd_in ns_in nd_rest ns_rest)  ; disagree z-score
```

### Example

```
Group 0 on P1 (state funding formula):
  In-group:     45 agree, 3 disagree, 48 total  →  pa = 0.92
  Everyone else: 30 agree, 55 disagree, 85 total →  pa = 0.36

  Agree ratio:  0.92 / 0.36 = 2.56  (this group agrees 2.5x more than others)
  Z-score:      6.8  (highly statistically significant)

  → P1 is REPRESENTATIVE of Group 0
    "Group 0 is defined by strong support for the state funding formula"

Group 1 on P8 (3-year timeline sufficient):
  In-group:     8 agree, 42 disagree, 50 total  →  pd = 0.83
  Everyone else: 60 agree, 25 disagree, 85 total →  pd = 0.30

  Disagree ratio: 0.83 / 0.30 = 2.77  (this group disagrees 2.8x more)
  Z-score:        7.2

  → P8 is REPRESENTATIVE of Group 1 (via disagreement)
    "Group 1 is defined by opposition to the 3-year timeline"
```

The positions with the highest agree/disagree ratios (and significant z-scores) per group become the **defining positions** — the statements that most characterize what that group believes and what separates them from others.

### Representativeness Output

```json
{
  "repness": {
    "0": [
      {"tid": 5, "ra": 2.56, "pa": 0.92, "na": 45, "nd": 3, "ns": 48},
      {"tid": 7, "ra": 1.89, "pa": 0.78, "na": 37, "nd": 10, "ns": 48}
    ],
    "1": [
      {"tid": 12, "rd": 2.77, "pd": 0.83, "na": 8, "nd": 42, "ns": 50},
      {"tid": 5, "rd": 2.12, "pd": 0.75, "na": 12, "nd": 38, "ns": 50}
    ]
  }
}
```

**Source files:**
- Vote statistics: `math/src/polismath/math/repness.clj` (lines 78-82)
- Comparative stats: `math/src/polismath/math/repness.clj` (lines 85-100)
- Two-proportion z-test: `math/src/polismath/math/stats.clj`

---

## Step 7: Consensus Detection

Polis identifies positions where **all groups agree** — these represent common ground regardless of how people cluster on other issues.

```json
{
  "consensus": {
    "agree": [7, 15],
    "disagree": [22]
  }
}
```

- `agree`: position IDs where all groups have high agree rates
- `disagree`: position IDs where all groups have high disagree rates

In our context: "All three groups agree that the bill addresses a real problem (P7). The disagreement is about approach, not whether action is needed."

---

## Step 8: Group Vote Breakdown

Per-group vote tallies for every position:

```json
{
  "group-votes": {
    "0": {
      "5":  {"A": 45, "D": 3,  "S": 48},
      "12": {"A": 12, "D": 36, "S": 50},
      "13": {"A": 40, "D": 5,  "S": 48}
    },
    "1": {
      "5":  {"A": 15, "D": 40, "S": 58},
      "12": {"A": 42, "D": 8,  "S": 50},
      "13": {"A": 10, "D": 45, "S": 58}
    }
  }
}
```

- `A` = agree count, `D` = disagree count, `S` = total who saw it
- This enables per-group percentage breakdowns: "Group A: 94% agree on state formula; Group B: only 26% agree"

---

## Step 9: Results Storage

Everything is written to `math_main` as a single JSONB blob per conversation:

```sql
INSERT INTO math_main (zid, math_env, last_vote_timestamp, math_tick, data, caching_tick)
VALUES (?, ?, ?, ?, ?::jsonb,
        COALESCE((SELECT max(caching_tick) + 1 FROM math_main), 1))
ON CONFLICT (zid, math_env)
DO UPDATE SET
    modified = now_as_millis(),
    data = excluded.data,
    math_tick = excluded.math_tick,
    caching_tick = excluded.caching_tick;
```

The `math_tick` increments on every recomputation. Clients use it as an ETag for cache invalidation.

**The full `data` JSON blob contains:**

```json
{
  "n": 312,
  "n-cmts": 32,
  "lastVoteTimestamp": 1709827200000,
  "math_tick": 47,
  "in-conv": [1, 2, 3, 5, 8, ...],
  "user-vote-counts": {"1": 28, "2": 32, "3": 15, ...},
  "base-clusters": { ... },
  "group-clusters": [ ... ],
  "pca": { ... },
  "repness": { ... },
  "group-votes": { ... },
  "consensus": { ... }
}
```

**Additional tables updated:**
- `math_bidtopid` — base-cluster-ID to participant-ID mapping
- `math_ptptstats` — per-participant statistics (vote count, last activity)

**Source files:**
- Write function: `math/src/polismath/conv_man.clj` (lines 158-169)
- SQL upsert: `math/src/polismath/components/postgres.clj` (lines 323-338)

---

## Step 10: Results Retrieval via API

**Endpoint:** `GET /api/v3/math/pca2`

```
GET /api/v3/math/pca2?conversation_id=bill_FL_HB123&math_tick=-1

Headers:
  If-None-Match: "46"   (client's last-known math_tick)
```

**Server handling** (`server/src/routes/math.ts`, lines 32-141):

1. Check in-memory LRU cache (300 conversations, 3-second TTL)
2. On cache miss: query PostgreSQL for `math_main` JSONB
3. Parse and normalize the JSON structure via `processMathObject()`
4. Fill any missing fields via `ensureCompletePcaStructure()`
5. Gzip the JSON and cache it
6. Return with ETag header for future conditional requests

```typescript
res.set({
  "Content-Type": "application/json",
  "Content-Encoding": "gzip",
  Etag: '"' + data.asPOJO.math_tick + '"'
});
res.send(data.asBufferOfGzippedJson);
```

**Cache behavior:**
- `304 Not Modified` if client's ETag matches current `math_tick`
- `304` also returned if no math results exist yet (polling)
- `200` with full gzipped JSON when new data is available

**Source files:**
- Handler: `server/src/routes/math.ts` (lines 32-141)
- PCA cache: `server/src/utils/pca.ts` (lines 320-422)

---

## Step 11: The Delphi Enhancement Layer

On top of the Clojure math results, Delphi adds richer analysis. It can run as a second pass on the same vote data.

**Delphi's Python math pipeline** (`delphi/polismath/run_math_pipeline.py`):

1. Fetches votes from PostgreSQL (same data, with sign flip: PostgreSQL AGREE=-1 → Delphi AGREE=+1)
2. Builds the same vote matrix
3. Runs PCA (power iteration, same algorithm ported to Python)
4. Runs K-means clustering
5. Computes representativeness

**Delphi's UMAP/Narrative pipeline** (`delphi/umap_narrative/run_pipeline.py`) adds:

1. **SentenceTransformer embeddings** (384-dim) of each statement's text
2. **UMAP projection** — nonlinear 2D projection, better than PCA for complex opinion spaces
3. **EVOC hierarchical clustering** — multi-layer topic clusters
4. **LLM topic naming** — human-readable cluster labels via Ollama or Anthropic
5. **Narrative synthesis** — per-cluster narratives via Anthropic Batch API

**Results stored in DynamoDB** (13 tables with `Delphi_` prefix):
- `Delphi_PCAResults` — PCA/cluster data
- `Delphi_CommentClustersLLMTopicNames` — topic labels per cluster
- `Delphi_NarrativeReports` — generated narrative reports
- `Delphi_CommentHierarchicalClusterAssignments` — multi-layer cluster memberships

**Served via Polis server routes:**
- `GET /api/delphi?report_id=...` — topic names and cluster data
- `GET /api/delphi/reports?report_id=...` — narrative reports

---

## What This Gives Us for Reporting

### Direct Metrics from `math_main`

| Metric | Source Field | Example Output |
|--------|-------------|----------------|
| Total participants | `n` | "312 people weighed in on HB 123" |
| Number of opinion groups | `group-clusters.length` | "3 distinct opinion groups" |
| Group sizes | base-cluster counts per group | "Group A: 58%, Group B: 32%, Group C: 10%" |
| What defines each group | `repness[gid]` | "Group A strongly supports the state funding formula (92% agree)" |
| Where everyone agrees | `consensus.agree` | "All groups agree the bill addresses a real problem" |
| Most divisive positions | `comment-extremity` (highest values) | "The most divisive issue is the enforcement timeline" |
| Per-position breakdown by group | `group-votes[gid][tid]` | "On funding: Group A 94% agree, Group B 26% agree" |
| 2D visualization data | PCA projections per participant | Scatter plot with group coloring |
| Which positions drive the clustering | PCA `comps` loadings | "Funding positions explain 42% of total variance" |
| Position importance | `comment-extremity` | "P8 (timeline) has extremity 0.91 — highest in the conversation" |

### Enriched Metrics (Our Opinion Landscape Adds Meaning)

Because every position ID maps back to our `PolicyPosition` → `BillTopic` → bill section structure, the raw math results gain semantic meaning:

```
repness says Group 0's defining positions are tid=5 and tid=7

  tid=5 → PolicyPosition "Fund through state formula"
       → BillTopic "Funding Approach"
       → Bill sections 3 and 7
       → Pro framing: "Increases per-pupil funding to match inflation"
       → Counter framing: "Creates unfunded mandate for local districts"

  tid=7 → PolicyPosition "Implement 3-year rollout"
       → BillTopic "Implementation Timeline"
       → Bill section 12

Report: "Group A (58% of participants) is defined by support for the bill's
state funding formula (Section 3) and the proposed 3-year implementation
timeline (Section 12). They believe the formula 'increases per-pupil
funding to match inflation' — while Group B argues it 'creates an
unfunded mandate for local districts.'"
```

### Delphi Narrative Enhancement

With Delphi narratives, VoteBot can quote polished summaries:

> "Based on 312 participants, there are three main opinion groups on HB 123.
> The largest group (58%) — 'Pragmatic Supporters' — backs the bill's funding
> formula but wants a longer implementation window. As one participant put it:
> 'The goals are right but three years isn't enough time for districts to adapt.'
> The second group (32%) — 'Local Control Advocates' — prefers property-tax-based
> funding and opposes state-level mandates. Both groups agree that current
> funding levels are inadequate — the disagreement is about mechanism, not need."

---

## Key Integration Insight

**We don't need to modify the Polis math pipeline at all.**

Our policy positions become Polis seed comments. Chat-extracted opinions become Polis votes. The existing Clojure math service computes PCA, clusters, and representativeness identically — it doesn't know or care whether a vote came from a Polis click or a VoteBot chat extraction.

The entire analysis pipeline is already built. We just need to:

1. **Get the data in** — positions as seed comments, opinions as votes (with weight encoding confidence)
2. **Get the results out** — read `math_main` via `GET /api/v3/math/pca2`
3. **Map results back to our semantic structure** — position IDs → PolicyPosition → BillTopic → bill sections → human-readable framing

The opinion landscape metadata (topics, position framings, bill section references) is what transforms raw cluster numbers into meaningful, reportable insights about public opinion on legislation.
