# Integration Plan: VoteBot as Guided Opinion Elicitation Tool

## Vision

VoteBot becomes a **guided opinion elicitation tool** for legislation. When users chat about a bill, VoteBot doesn't just answer questions — it understands the landscape of policy positions on that bill and helps users articulate where they stand. Users can select from known positions, express nuance, or introduce entirely new perspectives. Their opinions feed a shared opinion space alongside direct Polis voters, producing a living map of public sentiment on every tracked bill.

The chatbot is the primary modality. Polis provides a complementary direct-voting experience for users who prefer structured participation. Both inputs converge in a single, multi-dimensional opinion vector space where clustering reveals the real fault lines in public opinion.

**What makes this different from a survey:** The conversation is natural. Users don't see a form — they chat. VoteBot surfaces relevant policy positions contextually, when the conversation naturally turns to a topic. Users can push back, add nuance, or raise concerns the system hasn't seen before. The elicitation feels like a conversation with a knowledgeable person, not a questionnaire.

### Long-Term Goal: From Opinion to Legislation

The full vision extends beyond opinion mapping. The end goal is a pipeline where verified voter conversations produce not just "here's what people think" but actionable legislative output:

```
Voter conversations → Opinion vectors → Clustering & consensus
    → Policy directives → Draft amendments / new legislation
```

**This plan covers the first three stages** — opinion elicitation, clustering, and reporting results on the DDP website. The legislative drafting stage is not yet specified and will depend on emerging tools for AI-assisted legislative drafting. However, every design decision in this plan is made with the drafting stage in mind: opinion stances trace back to specific bill sections, novel claims capture what voters want that the bill doesn't address, and the data model is tool-agnostic so a future drafting service can consume it.

See "Phase 7: Future — Legislative Drafting Pipeline" at the end of this document for the conceptual outline of how the drafting stage would connect.

---

## Core Model: Multi-Position Opinion Vectors

### Why Not Single-Dimension Topics

A single "funding stance" dimension (-1 to +1) can't distinguish between:
- "Fund through income tax increases"
- "Fund through reallocation from other programs"
- "Tie funding to local property taxes"
- "Reduce funding overall"

These are all different positions on the same topic. People who agree on "more funding" might deeply disagree on *how*. A single dimension collapses this. We need multiple dimensions per topic.

### The Representation

Each topic in a bill has 3-6 **policy positions** — distinct stances that real people hold. Each position is one dimension of the opinion vector. A user's opinion is encoded as their agreement/disagreement with each position:

```
Bill: HB 123 (Education Reform)

Topic: Funding Approach
  Positions:
    P1: "Fund through the bill's proposed state formula"
    P2: "Tie funding to local property tax base instead"
    P3: "Fund through reallocation, not new revenue"
    P4: "Reduce overall education spending"

Topic: Teacher Requirements
  Positions:
    P5: "Raise certification standards as proposed"
    P6: "Focus on alternative certification pathways"
    P7: "Let districts set their own standards"

Topic: Implementation Timeline
  Positions:
    P8: "Three-year rollout is appropriate"
    P9: "Need 5+ years for districts to adapt"
    P10: "Should take effect immediately"
```

A user's opinion vector across all positions:

```
           P1    P2    P3    P4    P5    P6    P7    P8    P9    P10
user_1:  [+0.8, -0.3, +0.5,  0,  +0.7, -0.2,  0,  -0.6, +0.9,  0 ]
user_2:  [ 0,   +0.9,  0,  -0.7,  0,   +0.8, +0.3,  0,    0,  +0.6]
user_3:  [-0.5,  0,    0,  +0.8, -0.9,  0,   +0.7, +0.4,  0,  -0.3]
```

Positions aren't mutually exclusive — a user can support both P1 and P3 (the bill's formula AND reallocation). PCA finds the real axes of disagreement, which might not align with any single topic.

### Compatibility with Polis

Each policy position maps to a Polis seed statement. Polis voters agree/disagree with statements = filling in the same vector with +1/-1/0. Chat users fill it with continuous values extracted from conversation. Same coordinate space, different granularity.

```
Polis voter:  P1=+1, P2=-1, P3=0, P4=-1, ...  (discrete)
Chat user:    P1=+0.8, P2=?, P3=+0.5, P4=?, ...  (continuous, sparse)
```

The sparsity difference is acceptable — PCA and UMAP handle missing values, and chat users typically cover fewer positions than deliberate Polis voters. The clustering still works because even partial vectors carry signal.

---

## System Architecture

```
    DDP Website — Bill Page
    +--------------------------------------------+
    |                                            |
    |  +------------------+  +----------------+  |
    |  | VoteBot Chat     |  | Polis Embed    |  |
    |  | (primary input)  |  | (optional)     |  |
    |  |                  |  |                |  |
    |  | Guided           |  | Direct vote    |  |
    |  | elicitation      |  | on statements  |  |
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
                  Opinion Clusters
                  + Narratives
                        |
              +---------+---------+-----------------+
              v                   v                 v
        VoteBot responses    Polis visualization    DDP Website
        ("others think...")  (PCA opinion map)      (opinion reports)
                                                    |
                                            - - - - v - - - - - -
                                            :                   :
                                            : [FUTURE]          :
                                            : Legislative       :
                                            : Drafting Service  :
                                            :                   :
                                            : Consensus +       :
                                            : bill sections     :
                                            :       ↓           :
                                            : Policy directives :
                                            :       ↓           :
                                            : Draft amendments  :
                                            :                   :
                                            - - - - - - - - - - -
```

---

## Phase 1: Bill Analysis — Topics, Positions, and the Opinion Landscape

### What This Phase Does

When a bill is synced to VoteBot, an LLM analyzes the legislation and produces the **opinion landscape**: the set of topics and the range of real policy positions people hold on each.

### The Opinion Landscape

```python
class PolicyPosition:
    position_id: str            # "hb123_funding_state_formula"
    topic_id: str               # "hb123_funding"
    text: str                   # "Fund schools through the bill's proposed state formula"
    short_label: str            # "State formula (as proposed)"
    framing: str                # How a supporter would explain this position
    counter_framing: str        # The strongest argument against it
    bill_sections: list[str]    # Which sections of the bill support this position
    source: str                 # "bill_text" | "org_position" | "emergent"
    polis_tid: int | None       # Corresponding Polis statement ID, once created

class BillTopic:
    topic_id: str               # "hb123_funding"
    bill_webflow_id: str
    name: str                   # "Funding Approach"
    description: str            # Neutral description of what's at stake
    positions: list[PolicyPosition]  # 3-6 positions spanning the opinion space
    bill_sections: list[str]
    created_at: str

class OpinionLandscape:
    bill_webflow_id: str
    bill_title: str
    jurisdiction: str
    topics: list[BillTopic]     # 5-10 topics, each with 3-6 positions
    total_positions: int        # Sum of positions across topics (target: 25-50)
    version: int                # Increments when new positions/topics are added
    created_at: str
    updated_at: str
```

### Generating the Landscape

**Step 1: Topic extraction from bill text**

```
Analyze this legislation and identify the 5-10 key aspects that
people would have opinions about.

For each topic:
- Name (2-5 words)
- Neutral description of what the bill proposes
- Which sections of the bill address this

Focus on substantive provisions, not procedural details.
Identify topics where reasonable people genuinely disagree.
```

**Step 2: Position generation per topic**

```
For the topic "{topic_name}" in {bill_title}, generate 3-6 distinct
policy positions that real people hold.

Requirements:
- Each position is a genuine stance someone would advocate for
- Positions should span the realistic opinion space
- Include at least one position that supports the bill's approach
- Include at least one position that opposes or offers an alternative
- Positions are NOT just "support" and "oppose" — they represent
  different APPROACHES and PRIORITIES
- Each position should be a clear, concrete policy preference
  (not vague values like "fairness" or "freedom")
- Write each as a statement someone would agree with (under 100 chars)

Also provide:
- A short label (2-4 words) for UI display
- How a supporter would frame this position (1 sentence)
- The strongest counter-argument (1 sentence)
```

**Step 3: Enrichment from organization positions**

VoteBot already has org support/oppose data from Webflow CMS. These real-world positions anchor the landscape:

```
Topic: Funding Approach
  Position from bill text: "Fund through state formula as proposed"
  Position from Teachers Union: "Increase funding but index to inflation"  <-- NEW
  Position from Taxpayer Alliance: "Cap spending at current levels"  <-- NEW
```

Org-sourced positions get `source: "org_position"` and inherit credibility from being real advocacy positions.

### Storage

```
Redis (hot cache):
  votebot:landscape:{bill_webflow_id} -> JSON OpinionLandscape
  votebot:landscape:version:{bill_webflow_id} -> int (for cache invalidation)

PostgreSQL (durable):
  opinion_landscapes table -> Full landscape with version history
```

### When to Generate

- **On bill sync** (during existing scheduler job). Piggybacks on bill text ingestion.
- **Re-generate on bill version change** (existing `BillVersionSyncService` detects this). New text may introduce new provisions = new topics.
- **Incrementally update** when emergent positions are discovered from chat (Phase 5).

### Questions to Consider

1. **How many total positions across all topics?** Too few (<15) and the opinion space is underspecified — clustering won't find meaningful groups. Too many (>60) and the vector is too sparse from chat input. Target: 25-50 total positions across 5-10 topics, averaging 4-5 per topic.

2. **Should positions be mutually exclusive within a topic?** In the gun policy example, someone could support BOTH assault weapons ban AND red flag laws. If positions are independent (non-exclusive), you get richer signal but a wider vector. If exclusive (pick one), you get cleaner signal but lose nuance. Recommendation: **non-exclusive** — let people agree with multiple positions within a topic. The clustering will figure out which combinations are common.

3. **How do you ensure positions are genuinely distinct?** The LLM might generate "increase funding" and "more money for schools" — semantically identical. Embed all positions and reject any pair with cosine similarity > 0.85.

4. **Should the bill's actual provisions be treated as a position?** "Implement the bill as written" is always a valid position. Should it be auto-generated for every topic?

---

## Phase 2: Guided Elicitation in VoteBot Chat

### What This Phase Does

VoteBot becomes aware of the opinion landscape for each bill. During natural conversation, it detects when a user expresses or approaches an opinion, and surfaces relevant positions to help them articulate their stance.

### How It Works: Three Modes

#### Mode 1: Passive Extraction (Always On)

Every chat message is analyzed for opinion signals, regardless of whether the user is "in elicitation mode." This is the async post-response extraction from the original plan.

```
User: "I think this bill needs way more funding than what they proposed"

VoteBot answers the question normally.

Background extraction:
  Topic matched: "Funding Approach"
  Position matched: None exactly, but closest to
    "Increase funding but index to inflation" (similarity: 0.72)
  Stance: supports MORE funding than bill proposes
  Extracted vector: P1=-0.3, P2=+0.6, P3=-0.5, P4=-0.9
  Confidence: 0.78
```

This runs on every message at zero latency cost. Opinions are stored even if the user never sees the elicitation UI.

#### Mode 2: Contextual Prompting (Natural Conversation)

When VoteBot detects that a user has expressed interest in or opinions about a topic, it can naturally introduce the range of positions:

```
User: "What are people saying about the funding in this bill?"

VoteBot: "The funding provisions in HB 123 have been one of the most
discussed aspects. People generally fall into a few camps:

  - Support the bill's proposed state funding formula
  - Prefer tying funding to local property taxes
  - Want increased funding but through reallocation, not new revenue
  - Think overall education spending should be reduced

Based on 234 people who've weighed in, the first two positions have
the most support, but there's a notable group advocating for
reallocation. Where do you come down on this?"
```

The user might:
- **Select a position**: "I'm in the reallocation camp" → direct vector entry
- **Express nuance**: "I like the state formula but think it needs inflation indexing" → LLM maps to positions + captures the nuance
- **Introduce something new**: "None of those — I think funding should come from a lottery system" → flagged as emergent position
- **Decline to engage**: "I was just curious" → no opinion recorded

This is the **guided elicitation** — VoteBot presents the landscape, the user navigates it conversationally.

#### Mode 3: Explicit Elicitation (Opt-In)

A user could explicitly ask VoteBot to walk them through the bill's key issues:

```
User: "Help me figure out where I stand on this bill"

VoteBot: "Sure! HB 123 covers several key areas. Let's go through them.

First, on funding: this bill proposes a new state funding formula for
K-12 schools. Here are the main positions people hold:

  1. Support the bill's formula as written
  2. Prefer tying funding to local property taxes
  3. Fund through reallocation from other state programs
  4. Reduce overall education spending

Which of these resonates with you? Or do you see it differently?"

User: "Probably #1, but I also think reallocation makes sense"

VoteBot: "Got it — you support the bill's formula and are open to
reallocation rather than new revenue. Let me note that.

Next, on teacher certification requirements..."
```

This is the most structured mode — essentially a conversational survey. But because it's driven by the user asking for it, it doesn't feel intrusive.

### The Elicitation Prompt System

VoteBot needs new prompt context to enable guided elicitation. When a user is on a bill page:

```python
# In agent.py, during system prompt construction:
if page_context.type == "bill" and settings.elicitation_enabled:
    landscape = await opinion_service.get_landscape(page_context.webflow_id)
    if landscape:
        elicitation_context = format_landscape_for_llm(landscape)
        # Append to system prompt
```

The LLM system prompt addition:

```
## Opinion Landscape for {bill_title}

You are aware of the following policy positions that people hold on
this bill. When the user expresses an opinion or asks about public
sentiment, you may reference these positions to help them articulate
their views.

DO:
- Surface relevant positions when the conversation naturally turns
  to a topic
- Present positions neutrally, without advocating for any
- Ask which position resonates when the user seems engaged
- Accept nuanced responses that don't fit neatly into one position
- Note when a user introduces a perspective not listed here

DO NOT:
- Force elicitation — if the user is asking factual questions, answer them
- Present this as a survey or questionnaire
- Push the user to express opinions they haven't volunteered
- Limit the user to only the listed positions

Topics and positions:
{formatted_landscape}
```

### Opinion Signal Schema (Updated)

```python
class PositionStance:
    position_id: str            # References a PolicyPosition
    stance: float               # -1.0 to +1.0 (disagree to agree)
    confidence: float           # 0-1 confidence in extraction
    source: str                 # "explicit_selection" | "inferred" | "contextual"
    evidence: str               # The user's words that support this

class OpinionSignal:
    signal_id: str
    visitor_id: str             # Persistent device identity (localStorage)
    session_id: str
    conversation_id: str        # From analytics conversation tracking
    bill_webflow_id: str
    jurisdiction: str
    user_message: str
    bot_response: str
    elicitation_mode: str       # "passive" | "contextual" | "explicit"
    position_stances: list[PositionStance]
    novel_claims: list[str]     # Opinions that don't match any position
    timestamp: str
```

**Source field matters for weighting:**
- `explicit_selection`: User directly chose a position ("I'm in camp #1") — highest confidence
- `contextual`: User responded to a prompted range — high confidence
- `inferred`: Background extraction from unprompted conversation — moderate confidence

### Building the Opinion Vector Per Visitor

Across sessions, a visitor (identified by `visitor_id`) builds up an opinion vector incrementally. Within a session, the analytics system tracks `conversation_id` and `conversation_message_index`, providing natural scoping for extraction. Across sessions, the vector accumulates — a user who discussed funding last week and returns to discuss teacher certification today gets both sets of stances in their vector.

```
Message 1: "The funding formula seems reasonable"
  -> P1 (state formula): +0.6 (inferred, confidence 0.7)

Message 4: User responds to contextual prompt about enforcement
  -> P8 (3-year timeline): -0.8 (explicit_selection, confidence 0.95)
  -> P9 (5+ years): +0.7 (explicit_selection, confidence 0.95)

Message 7: "None of those options for teacher certification —
            I think national standards should apply"
  -> P5, P6, P7: no match
  -> novel_claim: "National teacher certification standards"

Final session vector:
  P1=+0.6  P2=?  P3=?  P4=?  P5=?  P6=?  P7=?  P8=-0.8  P9=+0.7  P10=?
  + 1 novel claim flagged for emergent position discovery
```

**Conflict resolution:** If a visitor expresses contradictory stances on the same position (within a session or across sessions), take the most recent one (people refine their views over time). If an explicit selection contradicts an earlier inference, the explicit one wins. Cross-session conflicts are resolved the same way — the vector keyed by `visitor_id` is always updated to reflect the latest stance.

### Questions to Consider

5. **How aggressively should VoteBot elicit?** There's a spectrum from "only surface positions when explicitly asked" to "proactively ask about every topic." Too aggressive feels like a survey. Too passive captures few opinions. Recommendation: contextual prompting when the user shows interest in a topic, but never push unprompted.

6. **Should VoteBot reveal how many people hold each position?** "234 people have weighed in; 42% support the state formula" gives social proof but could anchor opinions. Showing counts might bias toward popular positions. Option: show counts only AFTER the user has expressed their own view.

7. **What if the user doesn't want to express opinions?** Some users come to VoteBot purely for information. The elicitation should be invisible to them. The key indicator: does the user use opinion language ("I think", "I believe", "that's wrong") or information-seeking language ("what does", "how would", "explain")?

8. **Should the conversation history persist across sessions?** If a user discussed funding yesterday and returns today, should VoteBot remember? Sessions are tab-scoped (30-min timeout), but we now have `visitor_id` (localStorage-based, persists across sessions on the same browser). This enables cross-session opinion vector accumulation without authentication — opinion vectors can be keyed by `visitor_id` and built up over multiple visits. The limitation is that `visitor_id` tracks browser instances, not people (cleared by incognito, Safari ITP, storage clearing, not shared across devices). Full authenticated identity would be needed for revision flows and cross-device continuity.

9. **How do you prevent the LLM from editorializing?** VoteBot must present positions neutrally. But LLMs have tendencies. The system prompt needs strong guardrails, and the position framings (pro + counter) help balance.

---

## Phase 3: Polis Bridge — Shared Opinion Space

### What This Phase Does

Creates and maintains the Polis conversation for each bill, maps policy positions to Polis seed statements, and ensures both modalities feed the same opinion vector space.

### Conversation Setup

```
OpinionLandscape generated (Phase 1)
    |
    v
Create Polis conversation
    POST /api/v3/conversations
    topic: "{bill_title} — Public Input"
    conversation_id: "bill_{jurisdiction}_{number}"
    |
    v
For each PolicyPosition in the landscape:
    POST /api/v3/comments
    txt: position.text
    is_seed: true
    Store: position.polis_tid = response.tid
    |
    v
Redis mappings:
    votebot:polis:bill:{bill_webflow_id} -> conversation_id
    votebot:polis:positions:{bill_webflow_id} -> {position_id: polis_tid}
```

### Projecting Chat Users into Polis

When a chat user confirms their opinions (consent flow):

```
Chat visitor's opinion vector (keyed by visitor_id):
  votebot:opinions:{visitor_id}:{bill_webflow_id} -> [P1=+0.6, P8=-0.8, P9=+0.7, ...]

Discretize to Polis votes:
    > +0.3 -> agree (+1)
    < -0.3 -> disagree (-1)
    else   -> pass (0)

Submit to Polis:
    POST /api/v3/votes {tid: P1.polis_tid, vote: +1}
    POST /api/v3/votes {tid: P8.polis_tid, vote: -1}
    POST /api/v3/votes {tid: P9.polis_tid, vote: +1}
```

### Projecting Polis Voters into the Unified Space

Polis voters produce discrete +1/-1/0 per statement. These map directly to the opinion vector since each statement IS a policy position. No aggregation needed — the vector dimensions are already 1:1 with Polis statements.

### Identity & Authentication

VoteBot now has a three-level identity model (implemented):

| Level | ID | Lifetime | Use in Opinion System |
|---|---|---|---|
| **Visitor** | `visitor_id` | localStorage (permanent, best-effort) | Key for opinion vector accumulation across sessions. Enables returning users to build up their vector over multiple visits without auth. |
| **Session** | `session_id` | sessionStorage (per-tab, 30-min timeout) | Groups messages within a single visit. |
| **Conversation** | `conversation_id` | Server-side (resets on page/topic change) | Scopes opinion extraction to a coherent discussion thread. |

**Polis XID mapping:** Use `visitor_id` (not `session_id`) as the Polis external identity so that a returning visitor's Polis votes accumulate correctly:
- VoteBot service account created in Polis (one-time)
- Each chat user gets XID: `"votebot_{visitor_id}"`
- JWT: `{type: "xid", conversation_id, uid: service_uid, xid: "votebot_{visitor_id}"}`
- RSA-256 signed

**Limitation:** `visitor_id` is best-effort device identity (cleared by incognito, Safari ITP, storage clearing). A user switching browsers or devices creates a new visitor. For revision flows ("I changed my mind") and cross-device continuity, authenticated user IDs would be needed — but `visitor_id` is sufficient for the initial implementation.

### When to Create the Polis Conversation

| Trigger | Pros | Cons |
|---------|------|------|
| **On landscape generation** (recommended) | Ready when users arrive; Polis voters can start immediately | Some conversations may never get traffic |
| **On first confirmed opinion** | Only creates what's needed | Polis embed won't work until first chat user opines |
| **Manual** | Full control | Doesn't scale |

### Questions to Consider

10. **Should Polis voters see the topic groupings?** Polis normally presents statements in random/priority order. Grouping by topic could help voters think systematically, but Polis doesn't natively support topic-grouped presentation. This might require Polis UI modifications.

11. **One conversation per bill or per topic?** One per bill keeps everything together and enables cross-topic clustering. One per topic gives cleaner Polis UX but fragments the opinion space. Recommendation: one per bill.

---

## Phase 4: Clustering and Results

> **Technical reference:** For the complete data flow through Polis's math pipeline — vote matrix construction, PCA (power iteration), k-means (two-level), representativeness analysis, consensus detection, and results storage — see `plans/PLAN-polis-math-pipeline.md`. That document traces every step with exact code references. The key insight: **we don't need to modify the Polis math pipeline at all** — our policy positions become seed comments, chat opinions become weighted votes, and the existing Clojure math service computes everything identically.

### What This Phase Does

Runs PCA/UMAP and k-means on the unified opinion matrix to find opinion groups, then surfaces results in both VoteBot and Polis.

### The Unified Matrix

```
                    P1    P2    P3    P4    P5    P6  ...  P10   P_new1
polis_user_1:      [+1    -1     0    -1    +1     0        0      ?  ]
polis_user_2:      [-1    +1    +1     0     0    +1       -1      ?  ]
v_fcfdd902b491:    [+0.6   ?     ?     ?     ?     ?      +0.7    ?  ]  (chat, cross-session)
v_a1b2c3d4e5f6:    [ ?    +0.9   ?    -0.7  +0.8   ?       ?     +0.6]  (chat, confirmed)
confirmed_5:       [+1     0    +1    -1    +1    -1       +1     -1  ]
```

- Polis voters: dense, discrete (+1/-1/0)
- Chat visitors (unconfirmed): sparse, continuous, lower weight — keyed by `visitor_id`, accumulated across sessions
- Chat visitors (confirmed): sparse, discretized, full weight — `visitor_id` links to Polis XID

### Clustering Pipeline

Run as a Delphi job (leverages existing infrastructure):

```
Job type: OPINION_LANDSCAPE_CLUSTERING
Input: bill_webflow_id
Steps:
  1. Fetch Polis vote matrix for bill's conversation
  2. Fetch confirmed + unconfirmed chat opinion vectors from Redis
  3. Merge into unified matrix
  4. Handle missing values (impute with 0 or use masking)
  5. Run PCA (2D projection for visualization)
  6. Run k-means (k=2-5, selected by silhouette score)
  7. For each cluster:
     - Compute mean stance per position
     - Identify defining positions (highest variance between clusters)
     - Generate narrative label via LLM
     - Select representative quotes from chat users in cluster
  8. Store results (Delphi writes to its DynamoDB tables)
  9. Cache summary in Redis for VoteBot
```

### Weighting

| Source | Weight | Rationale |
|--------|--------|-----------|
| Polis vote (explicit) | 1.0 | Deliberate, unambiguous |
| Chat opinion (confirmed via consent) | 0.9 | User verified the extraction |
| Chat opinion (explicit selection in elicitation) | 0.85 | Direct but through LLM intermediary |
| Chat opinion (contextual prompt response) | 0.7 | High confidence but conversational |
| Chat opinion (passive inference) | 0.5 | LLM-extracted, no user verification |

### Cluster Output

```python
class OpinionCluster:
    cluster_id: int
    size: int
    percentage: float
    label: str                          # "Supportive but cost-conscious"
    defining_positions: list[dict]      # Positions that most distinguish this cluster
    mean_stances: dict[str, float]      # Average stance per position
    representative_quotes: list[str]    # Best chat excerpts
    polis_repness: list[dict]           # Most representative Polis statements

class ClusteringResult:
    bill_webflow_id: str
    total_participants: int
    n_clusters: int
    clusters: list[OpinionCluster]
    consensus_positions: list[str]      # Positions all clusters agree on
    divisive_positions: list[str]       # Positions that most separate clusters
    pca_explained_variance: list[float] # How much each PC explains
    last_computed: str
```

### Surfacing Results in VoteBot

```python
# In agent.py, alongside existing pre-fetch:
if page_context.type == "bill" and settings.elicitation_enabled:
    clusters = await opinion_service.get_clustering_result(
        page_context.webflow_id
    )
    if clusters and clusters.total_participants >= MIN_PARTICIPANTS:
        cluster_context = format_clusters_for_llm(clusters)
```

**Example VoteBot response with cluster data:**

> User: "What do people think about the funding in this bill?"
>
> VoteBot: "312 people have shared their views on HB 123. On funding specifically, there are two main groups:
>
> **Group A (58%)** — 'Pragmatic supporters': They support the bill's state funding formula but want spending caps to control costs. As one participant put it: 'The formula is sound but we need guardrails on total expenditure.'
>
> **Group B (42%)** — 'Local control advocates': They prefer tying funding to local property taxes and oppose a one-size-fits-all state formula. A common view: 'Districts know their needs better than the state does.'
>
> Both groups agree that current funding levels are inadequate — the disagreement is about mechanism, not need.
>
> Where do you fall on this?"

### Recomputation Frequency

- **Daily** at 05:00 UTC (after bill version sync at 04:00 UTC)
- **Threshold-triggered**: When 20+ new confirmed opinions arrive since last run
- **On-demand**: Admin trigger

### Questions to Consider

12. **What's the minimum participant count for meaningful clusters?** With a 30-dimensional vector, you probably need 50+ participants for PCA to find stable axes. Below that, show aggregate stance distributions per position without clustering.

13. **Should passive (unconfirmed) chat opinions be included in clustering?** They add volume but introduce noise from extraction errors. Options: include at low weight (0.5), exclude entirely, or include only above a confidence threshold (0.8+).

14. **How do you handle the Polis/chat density asymmetry?** A Polis power user votes on all 30 positions. A casual chat user covers 3. Should sparse users pull the clusters less? PCA naturally handles this, but consider whether to set a minimum coverage threshold (e.g., must have stances on 5+ positions).

    **Critical constraint from Polis math pipeline:** The Polis Clojure math service requires participants to have voted on **at least 7 statements** to be included in the "in-conv" set used for PCA and clustering (see `PLAN-polis-math-pipeline.md` Step 3 — `conversation.clj` filtering logic). Chat users who cover only 3-5 positions will be **excluded from clustering entirely**. This means either:
    - **(a)** The elicitation flow must guide chat users to cover 7+ positions (e.g., walk them through at least 2-3 topics at 3-4 positions each), or
    - **(b)** The Polis minimum threshold must be lowered in our deployment (configurable in `conversation.clj`), or
    - **(c)** Chat users below 7 positions are included only in aggregate counts but not in cluster assignment — their opinions still reach Polis as votes and will be counted if they return and vote on more positions later (accumulation via `visitor_id`/`member_id`)

    Recommendation: **(c)** for v1 — accept that casual chat users may not meet the threshold, and rely on cross-session accumulation to build up their coverage over time. The Jigsaw explicit elicitation mode (Mode 3) naturally covers 7+ positions.

---

## Phase 5: Emergent Position Discovery

### What This Phase Does

Captures opinions from chat that don't match any existing policy position, clusters them, and generates new positions to expand the opinion landscape.

### Pipeline

```
Novel claims from chat (flagged in Phase 2)
    "Funding should come from a state lottery"
    "What about funding through public-private partnerships?"
    "We should fund this by cutting the highway budget"
    |
    v
[Accumulate until threshold]
    Minimum 5 distinct users with novel claims on same bill
    |
    v
[Cluster novel claims]
    Embed with SentenceTransformer
    Cluster with HDBSCAN
    |
    v
[For each cluster of 3+ claims:]
    LLM generates candidate PolicyPosition:
      - text, short_label, framing, counter_framing
    |
    v
[Quality gate]
    Semantic distance from all existing positions > 0.15
    Not too vague ("things should be better") — specificity check
    Clearly about the bill, not general political commentary
    |
    v
[Integration]
    Add to OpinionLandscape (source: "emergent")
    Generate Polis seed statement
    Submit to Polis: POST /api/v3/comments (is_seed=true)
    Backfill: re-score existing chat opinions against new position
    Next clustering run includes new dimension
```

### Why Bill-Scoped Topics Minimize Drift

Because the initial positions come from bill text, the coordinate system is anchored to something concrete. A bill's provisions don't change (unless amended). Emergent positions expand the space but don't move existing axes. The stable backbone (bill-derived positions) plus the controlled growth (emergent positions with high threshold) keeps drift manageable.

The architecture handles bill amendments naturally: when `BillVersionSyncService` detects new bill text, re-run Phase 1 landscape generation. New provisions become new topics/positions. Existing ones persist.

### Questions to Consider

15. **Should emergent positions require human approval?** Options: auto-approve above threshold (fast, scalable), queue for admin review (safe, slow), or auto-approve + notify admin (balanced). Recommendation: auto-approve + notify for the first version, tighten later if quality issues emerge.

16. **Can emergent positions from one bill inform another?** If users keep raising "impact on homeschool families" on education bills across multiple states, that's a pattern worth codifying. Cross-bill learning could improve initial landscape generation.

17. **What if an emergent position splits an existing one?** "Fund through the state formula" might split into "Fund through state formula with inflation indexing" vs. "Fund through state formula at fixed levels." Should the original position be retired and replaced with the two more specific ones? This changes the vector dimensions — need a versioning strategy.

---

## Phase 6: Consent, Authentication, and Voter Verification

### The Consent-to-Auth Funnel

The opinion consent flow is the natural trigger for Memberstack account creation. Saving opinions requires an account; creating an account is the opportunity for voter file verification. The funnel has three stages:

```
Stage 1: Conversational consent          Stage 2: Account creation           Stage 3: Voter verification
(in-chat, lightweight)                   (Memberstack signup)                (voter file match)
+-----------------------------------+    +------------------------------+    +------------------------------+
| VoteBot summarizes extracted      |    | "Create a free account to    |    | "To verify you as a voter,   |
| opinions, user confirms/edits     |    |  save your views and see     |    |  we'll match your info        |
|                                   | -> |  how you compare to others"  | -> |  against your state's voter   |
| No account needed at this stage   |    |                              |    |  registration records"        |
| Opinions stored against           |    | Memberstack modal (Webflow-  |    |                              |
| visitor_id temporarily            |    | native, handles email/pass)  |    | Name + DOB + address match   |
+-----------------------------------+    +------------------------------+    +------------------------------+
        |                                         |                                   |
        v                                         v                                   v
  visitor_id + opinions              member_id linked to visitor_id        verified_voter = true
  (Redis, 90-day TTL)               (permanent, cross-device)             jurisdiction + district confirmed
                                    opinions promoted to durable           Polis XID = "votebot_{member_id}"
                                    storage                                full weight in clustering
```

### Stage 1: Conversational Consent (In-Chat)

After detecting opinions during a chat session, VoteBot presents a summary when the conversation reaches a natural pause:

```
VoteBot: "Based on our conversation, it seems like you have some
views on HB 123:

On funding: You support the state formula approach but want
inflation indexing.
On teacher certification: You prefer alternative pathways over
raising standards.
On timeline: You think 3 years isn't enough.

Does this sound right? You can edit anything, or skip entirely."

User: "Yeah that's about right, but I'm actually neutral on timeline"

VoteBot: "Got it — I've updated that.

Would you like to save your views and see how you compare to
the 312 other people who've weighed in on this bill? You'll
need a free DDP account to save them permanently."

User: "Sure"

VoteBot: "Great! I'll open the signup form. Once you're set up,
your views will be saved and you can update them anytime."
```

At this point, opinions are stored against `visitor_id` in Redis (90-day TTL). The user can close the widget and opinions persist on the same browser. But they're not yet durable, cross-device, or verified.

### Stages 2 & 3: Conversational Onboarding (Account + Voter Verification)

Instead of redirecting to a Memberstack modal or external page, VoteBot handles the entire onboarding conversationally — the user never leaves the chat. The flow has two sequential steps, each independently valuable:

**Step A: Name + Email → Memberstack account created → opinions saved**
**Step B: DOB + Zip → Catalist verification → voter status confirmed**

The user can stop after Step A and still have a working account with saved opinions. Step B is an optional upgrade that adds voter verification. This separation puts the low-friction ask first (name + email — most people give freely) and the higher-friction ask second (DOB + zip — requires more trust).

#### Conversational Flow

```
VoteBot: "Would you like to save your views and see how you
compare to others? I'll just need your name and email."

User: "Sure — I'm John Smith, john@example.com"

    ── Step A: server-side ──
    1. Parse name + email from message (LLM extraction)
    2. Memberstack Admin API: POST /members
       { email: "john@example.com", customFields: { name: "John Smith" } }
       → member_id = "mem_abc123"
    3. Link visitor_id → member_id in Redis (bidirectional, permanent)
    4. Promote opinions from visitor_id → member_id durable storage
    5. Submit confirmed opinions to Polis with XID = "votebot_mem_abc123"
    6. Memberstack sends password setup email automatically
    ────────────────────────

VoteBot: "Thanks, John! Your views on HB 123 are saved. 312
others have weighed in.

To verify that you're a registered voter in your district,
I just need your date of birth and zip code. This lets us
show legislators what their actual constituents think.
(This info is only used for verification and won't be stored.)"

User: "March 15, 1990, zip 20008"

    ── Step B: server-side ──
    1. Parse DOB + zip from message (LLM extraction)
    2. Catalist Fusion Light API: verify_voter(
         first_name="John", last_name="Smith",
         dob="1990-03-15", zip="20008")
       → DWID found, FL, CD-7, State Senate 12, State House 38
    3. Memberstack Admin API: PUT /members/{member_id}
       { customFields: {
           catalist_dwid: "51894720",
           verified_voter: true,
           jurisdiction: "FL",
           congressional_district: "7",
           state_senate_district: "12",
           state_house_district: "38"
       }}
    4. Update Redis member record with DWID + districts
    5. Opinions now carry verified voter weight (1.0x)
    ────────────────────────

VoteBot: "You're verified! ✓ Registered voter in Florida,
Congressional District 7.

Your views now carry full weight in our opinion maps.
63% of verified voters in your district share your stance
on the funding provisions.

(Check your email to set a password for future logins.)"
```

#### Why Two Steps, Not One

| Approach | Pros | Cons |
|---|---|---|
| All at once (name + email + DOB + zip) | Single exchange | Too much PII upfront; users may bail |
| **Step A then Step B (recommended)** | Low-friction first; natural off-ramp; account exists even if they skip verification | Two exchanges; slightly longer |
| Step B only (no account) | Minimal | No persistent identity; can't save opinions |

The key insight: **the Memberstack account is valuable without voter verification, but voter verification is useless without an account.** So create the account first.

#### If the User Declines Verification

```
VoteBot: "To verify that you're a registered voter in your
district, I just need your date of birth and zip code."

User: "I'd rather not"

VoteBot: "No problem! Your views are still saved and count
in the overall opinion map. You can verify anytime by saying
'verify my voter registration.'

Want to see how your views compare to others?"
```

The user is an **Active (member)** at 0.9x weight. Their opinions are in the system, just without district attribution or anti-gaming protection.

#### If Catalist Doesn't Find a Match

```
VoteBot: "I wasn't able to find a matching voter registration.
This can happen if you've recently moved, changed your name,
or aren't yet registered.

Your views are still saved! You can:
- Try again with different info (say 'verify again')
- Register to vote at vote.org
- Continue without verification

Would you like to try again?"
```

#### Server Implementation

```python
# services/memberstack.py

import httpx
import structlog

from votebot.config import get_settings

logger = structlog.get_logger()


async def create_member(email: str, name: str) -> dict | None:
    """Create a Memberstack member via the Admin API.

    Returns the created member data including member_id.
    The server creates the account directly — no client-side
    modal or redirect needed.
    """
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://admin.memberstack.com/members",
                headers={
                    "X-API-KEY": settings.memberstack_admin_api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "email": email,
                    "customFields": {"name": name},
                    "sendWelcomeEmail": True,  # Includes password setup link
                },
            )
            if resp.status_code in (200, 201):
                return resp.json()
            if resp.status_code == 409:
                # Member already exists with this email
                logger.info("Member already exists", email=email)
                return await get_member_by_email(email)
            logger.warning(
                "Memberstack member creation failed",
                status=resp.status_code,
                body=resp.text,
            )
    except Exception:
        logger.warning("Memberstack creation error", exc_info=True)
    return None


async def update_member_metadata(
    member_id: str, custom_fields: dict
) -> bool:
    """Update custom fields on a Memberstack member profile.

    Used after Catalist verification to store DWID + districts.
    """
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.put(
                f"https://admin.memberstack.com/members/{member_id}",
                headers={
                    "X-API-KEY": settings.memberstack_admin_api_key,
                    "Content-Type": "application/json",
                },
                json={"customFields": custom_fields},
            )
            return resp.status_code == 200
    except Exception:
        logger.warning("Memberstack update error", exc_info=True)
    return False


async def get_member_by_email(email: str) -> dict | None:
    """Look up an existing Memberstack member by email."""
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://admin.memberstack.com/members",
                headers={"X-API-KEY": settings.memberstack_admin_api_key},
                params={"email": email},
            )
            if resp.status_code == 200:
                members = resp.json().get("data", [])
                if members:
                    return members[0]
    except Exception:
        logger.warning("Memberstack lookup error", exc_info=True)
    return None
```

```python
# In api/routes/websocket.py — the agent handles the conversational
# extraction of name/email/DOB/zip from natural language. The structured
# data is passed to the onboarding service:

async def handle_onboarding_step_a(
    session_id: str,
    visitor_id: str,
    name: str,
    email: str,
):
    """Step A: Create Memberstack account, link to visitor, save opinions."""
    from votebot.services.memberstack import create_member

    member_data = await create_member(email=email, name=name)
    if not member_data:
        return {"success": False, "error": "Account creation failed"}

    member_id = member_data["id"]

    # Link visitor_id → member_id
    await redis_store.link_visitor_to_member(visitor_id, member_id)

    # Promote opinions
    await opinion_service.promote_opinions(visitor_id, member_id)

    # Submit to Polis
    await polis_service.submit_opinions(
        member_id=member_id,
        xid=f"votebot_{member_id}",
    )

    # Update session
    session = manager.get_session(session_id)
    if session:
        session["member_id"] = member_id
        session["member_name"] = name
        session["authenticated"] = True

    return {"success": True, "member_id": member_id}


async def handle_onboarding_step_b(
    session_id: str,
    member_id: str,
    first_name: str,
    last_name: str,
    dob: str,
    zip_code: str,
):
    """Step B: Verify voter via Catalist, update Memberstack profile."""
    from votebot.services.catalist import verify_voter
    from votebot.services.memberstack import update_member_metadata

    # Catalist verification
    result = await verify_voter(
        first_name=first_name,
        last_name=last_name,
        dob=dob,
        address1="",  # Not collected — zip is sufficient for matching
        city="",
        state="",     # Catalist infers from zip
        zip_code=zip_code,
    )

    if not result or not result.get("matched"):
        return {"success": False, "error": "no_match"}

    # Update Memberstack profile with verification data
    await update_member_metadata(member_id, {
        "catalist_dwid": result["dwid"],
        "verified_voter": True,
        "jurisdiction": result["state"],
        "congressional_district": result.get("congressional_district"),
        "state_senate_district": result.get("state_senate_district"),
        "state_house_district": result.get("state_house_district"),
    })

    # Update Redis
    await redis_store.set_member_verification(member_id, result)

    return {
        "success": True,
        "state": result["state"],
        "congressional_district": result.get("congressional_district"),
    }
```

#### How the LLM Extracts Structured Data

The agent needs to parse natural language into structured fields. This is handled by adding an **onboarding tool** to the LLM's tool set (same pattern as the existing `get_bill_info` tool):

```python
# Tool definition for the LLM:
{
    "type": "function",
    "function": {
        "name": "create_voter_account",
        "description": "Create a DDP account and optionally verify voter registration. Call this when the user provides their name and email to save their opinions.",
        "parameters": {
            "type": "object",
            "properties": {
                "first_name": {"type": "string"},
                "last_name": {"type": "string"},
                "email": {"type": "string", "format": "email"},
                "dob": {"type": "string", "description": "Date of birth (YYYY-MM-DD), if provided"},
                "zip_code": {"type": "string", "description": "5-digit zip code, if provided"}
            },
            "required": ["first_name", "last_name", "email"]
        }
    }
}
```

When the user provides name + email, the LLM calls `create_voter_account` with those fields (Step A). If the user later provides DOB + zip, the LLM calls it again with the additional fields (Step B). The server differentiates based on which fields are present.

#### Returning Members

When the widget loads and the user is already logged into Memberstack (detected via `$memberstackDom.getCurrentMember()` on the parent page), the widget sends the Memberstack token over WebSocket. The server validates it and links the session:

```javascript
// In widget.js, after WebSocket connects:
async function checkExistingAuth() {
    if (!window.$memberstackDom) return;

    var result = await window.$memberstackDom.getCurrentMember()
        .catch(function() { return null; });

    if (result && result.data && result.data.tokens) {
        DDPWebSocket.send({
            type: 'member_auth',
            payload: {
                memberstack_token: result.data.tokens.accessToken,
                visitor_id: DDPWebSocket.getVisitorId()
            }
        });
    }
}
```

For returning members, the server validates the token against Memberstack's API to get the trusted `member_id`:

```python
# services/memberstack.py

async def validate_memberstack_token(token: str) -> dict | None:
    """Validate a Memberstack JWT and return verified member data.

    Used for returning members who are already logged in.
    This is the ONLY way to get a trusted member_id from the client
    — never trust a bare member_id sent over WebSocket.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://admin.memberstack.com/members/current",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        logger.warning("Memberstack validation error", exc_info=True)
    return None
```

#### Identity Lifecycle Summary

| Scenario | How Identity is Established | Trust Level |
|---|---|---|
| **New anonymous user** | `visitor_id` from localStorage | Untrusted (device-level) |
| **User provides name + email in chat** | Server creates Memberstack account via Admin API → `member_id` | Server-trusted (server created it) |
| **User provides DOB + zip in chat** | Server calls Catalist → DWID returned | Server-trusted (Catalist verified) |
| **Returning member (already logged in)** | Widget sends Memberstack JWT → server validates → `member_id` | Server-trusted (token validated) |
| **User logs in via DDP website** | Memberstack SDK on page → widget detects → sends token | Server-trusted (token validated) |

**Key security principle:** The server either creates the identity itself (Steps A & B) or validates a cryptographic token (returning members). It never trusts a bare `member_id` from the client.

#### Configuration (new settings)

```python
# In config.py Settings:
memberstack_admin_api_key: str = ""   # Server-side Admin API key (not client SDK key)
```

### Stage 3: Voter File Verification via Catalist

During Memberstack onboarding (or as a subsequent step for existing members), the user is prompted to verify their identity against the national voter file via the **Catalist Fusion Light API**.

```
Memberstack signup flow (Webflow-native):
  Step 1: Email + password (standard Memberstack)
  Step 2: "Verify as a registered voter" (optional)

Voter verification form:
  - First name + Last name
  - Date of birth
  - Residential address (street, city, state, zip)
  - [Verify my registration]
```

#### Catalist Fusion Light API Integration

Catalist provides a national voter file matching service covering all 50 states + DC via a single API. The Fusion Light API matches input records against Catalist's voter file in real time and returns enriched data including a **DWID** (nationally unique person identifier), registration status, district assignments, and demographic/model data.

**Authentication:**
```python
# services/catalist.py

import httpx
import time
from votebot.config import get_settings

# Module-level token cache (24-hour validity)
_token_cache: dict = {"token": None, "expires_at": 0}

async def _get_catalist_token() -> str:
    """Get or refresh the Catalist API token.

    IMPORTANT: Catalist requires token reuse — do NOT request a new token
    per call. Tokens are valid for 24 hours. Catalist will suspend access
    for applications that request excessive tokens.
    """
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 300:
        return _token_cache["token"]

    settings = get_settings()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://auth.catalist.us/oauth/token",
            json={
                "grant_type": "client_credentials",
                "client_id": settings.catalist_client_id,
                "client_secret": settings.catalist_client_secret,
                "audience": "catalist_api_fusion_prod",
            },
            headers={"content-type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        _token_cache["token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + data["expires_in"]
        return _token_cache["token"]
```

**Voter verification call:**
```python
async def verify_voter(
    first_name: str,
    last_name: str,
    dob: str,          # YYYY-MM-DD
    address1: str,
    city: str,
    state: str,
    zip_code: str,
    address2: str | None = None,
) -> dict | None:
    """Match a person against the Catalist national voter file.

    Returns enriched voter data if matched, None if no match.
    The DWID is the unique deduplication key — it persists across
    address changes and state moves.
    """
    settings = get_settings()
    token = await _get_catalist_token()

    headers_list = [
        "firstName", "lastName", "dob",
        "address1", "address2", "city", "state", "zip"
    ]
    record = [
        first_name, last_name, dob,
        address1, address2 or "", city, state, zip_code
    ]

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"https://api.catalist.us/fusion/v1/workflow/"
            f"{settings.catalist_workflow_id}/job?token={token}",
            json={
                "executionType": "sync",
                "headers": headers_list,
                "records": [record],
            },
            headers={"content-type": "application/json"},
        )
        resp.raise_for_status()
        result = resp.json()

    # Extract matched record from immediateResult
    records = result.get("immediateResult", {}).get("records", [])
    if not records:
        return None

    matched = records[0]
    dwid = matched.get("DWID")
    if not dwid:
        return None

    return {
        "dwid": dwid,
        "matched": True,
        "state": matched.get("state"),
        "congressional_district": matched.get("congressional_district"),
        "state_senate_district": matched.get("state_senate_district"),
        "state_house_district": matched.get("state_house_district"),
        "registration_status": matched.get("registration_status"),
        "party_registration": matched.get("party_registration"),
        # Additional fields depend on workflow configuration
    }
```

**Verification pipeline:**

```
User submits verification info (name, DOB, address)
    |
    v
VoteBot API: POST /votebot/v1/voter/verify
    |
    v
CatalistService.verify_voter()
  1. Get/reuse 24-hour auth token
  2. POST to Fusion Light API (sync, single record)
  3. Catalist normalizes input, matches against national voter file
  4. Returns DWID + enriched voter data (districts, registration, etc.)
    |
    v
If DWID returned (match found):
  - Set member.verified_voter = true
  - Set member.catalist_dwid = DWID (unique, persistent person ID)
  - Set member.jurisdiction = matched state
  - Set member.congressional_district = from Catalist
  - Set member.state_senate_district = from Catalist
  - Set member.state_house_district = from Catalist
  - Update Memberstack metadata via API
  - Opinions now carry "verified voter" weight
    |
    v
If no match:
  - User can retry with corrected info
  - Common issues: name spelling, old address, recent move
  - Or continue as unverified member
  - Opinions still count, just without verified weight
```

#### Why Catalist / DWID

| Feature | Catalist DWID | State voter file IDs |
|---------|--------------|---------------------|
| **Coverage** | All 50 states + DC in one API | Per-state, inconsistent access |
| **Persistence** | Same DWID across address changes and state moves | Changes when voter re-registers |
| **Deduplication** | Nationally unique — one person, one DWID | Only unique within state |
| **Data richness** | Districts, registration, demographics, models | Varies by state |
| **API model** | Single REST call, sync, <1s latency | Varies: some API, some bulk file, some no access |

The DWID is the deduplication key for the opinion system. If someone creates two Memberstack accounts, both will resolve to the same DWID — only the most recent opinion vector counts.

#### Infrastructure Requirements

| Requirement | Details |
|---|---|
| **IP allowlisting** | EC2 public IP must be registered with Catalist (static Elastic IP required) |
| **Credentials** | `catalist_client_id`, `catalist_client_secret`, `catalist_workflow_id` in env/settings |
| **Workflow setup** | Catalist configures a workflow with person matching + voter file append. One-time setup with Catalist account rep. |
| **Token management** | 24-hour tokens cached in memory. Must NOT request new tokens per call — Catalist will suspend access. |
| **Rate limits** | Up to 20 records per sync request. For voter verification (one user at a time), single-record calls are sufficient. |

#### Configuration (new settings)

```python
# In config.py Settings:
catalist_client_id: str = ""
catalist_client_secret: str = ""
catalist_workflow_id: str = ""       # e.g., "wkfl-bvns34hg"
catalist_audience: str = "catalist_api_fusion_prod"
```

**What voter verification enables:**
- **Verified opinions carry full weight** in clustering — these are real constituents, not bots
- **District-level opinion maps** — "Here's what verified voters in Congressional District 12 think about HB 123"
- **Multi-level district attribution** — Congressional, state senate, and state house districts from a single Catalist match
- **Legislator accountability** — "63% of your verified constituents oppose this provision"
- **Anti-gaming** — one person, one DWID, one verified opinion per bill. Works across Memberstack accounts.
- **Persistence across moves** — if a voter moves, their DWID follows them. Re-verification updates district but preserves identity.

**What it does NOT do:**
- Store raw PII (name, DOB, address discarded after verification — only DWID + districts retained)
- Reveal how someone voted in elections (voter file = registration, not ballot)
- Require verification to use VoteBot or express opinions — always optional

### Participation Tiers (Updated)

| Tier | How Created | Identity Key | Weight | Capabilities |
|------|------------|-------------|--------|-------------|
| **Passive** | Any chat user who expressed opinions | `visitor_id` | 0.5x | Aggregate stats only ("312 discussions") |
| **Active (anonymous)** | Confirmed stances via in-chat consent | `visitor_id` | 0.7x | Included in clustering, can see own group |
| **Active (member)** | Confirmed + Memberstack account | `member_id` | 0.9x | Durable, cross-device, Polis votes submitted |
| **Verified voter** | Active member + voter file match | `member_id` + `voter_file_id` | 1.0x | Full weight, district attribution, anti-gaming |
| **Polis Direct** | Voted in Polis embed | Polis participant ID | 1.0x | Full weight in clustering |

Progression: **passive → active (anonymous) → active (member) → verified voter**. Each step is opt-in. `visitor_id` bridges anonymous stages; `member_id` bridges authenticated stages; `voter_file_id` provides civic verification.

### Questions to Consider

18. **When exactly should the consent prompt appear?** Recommendation: after 3+ opinion signals detected and a natural conversation pause. Too early is annoying. Too late and the user has left.

19. **Should the consent prompt be in-chat or a separate UI?** Recommendation: consent summary in-chat (conversational), then hand off to Memberstack modal for account creation. The two-step approach feels natural.

20. **Can a user revise after submitting?** Yes — `member_id` enables revision across devices and sessions. Returning members can update stances; Polis votes are resubmitted. Anonymous visitors can revise via `visitor_id` on the same browser.

21. **What about the passive tier ethically?** Recommendation: passive tier contributes only to aggregate counts ("312 discussions"), never to individual clustering or district-level reporting. Only confirmed opinions (Active+) enter the opinion vector space.

22b. **Should voter verification be required or optional?** Optional — requiring it would dramatically reduce participation. Unverified members still contribute valuable signal. Verification adds credibility weight and enables district attribution.

23b. **How do you handle voter file matching errors?** False positives mitigated by requiring multiple fields (name + DOB + address). False negatives happen with stale data or name spelling differences. Allow retry. Never block the user.

24b. **Privacy of voter file data?** PII (name, DOB, address) is sent to Catalist in transit only — not stored by VoteBot. Only the Catalist DWID (opaque person ID) + district assignments are retained. User can request deletion of their verified status via Memberstack account settings, which clears the DWID from our records.

25. **What if someone creates multiple Memberstack accounts?** Deduplicate by voter file ID — if two `member_id`s map to the same voter, only the most recent opinion vector counts. For unverified members, accept the limitation that multi-account gaming is possible (same as any web service without identity verification).

---

## Phase 7: Closing the Loop

### Polis on the DDP Site

For users who prefer structured voting, embed the Polis conversation on each bill page:

- Polis statements = policy positions from the opinion landscape
- Users can vote directly (standard Polis experience)
- If they also chatted with VoteBot, their confirmed opinions are pre-voted
- XID links the two identities

### VoteBot Awareness of Clusters

VoteBot uses cluster data to enrich every bill conversation:

- **"What do people think?"** → Direct cluster summary with representative quotes
- **After user expresses opinion** → "Your view aligns with 42% of participants who also prioritize local control"
- **Guided elicitation enrichment** → "Most people who support the state formula also support inflation indexing — what about you?"
- **Narrative summaries** → Delphi-generated narratives per cluster, quoted by VoteBot

### Bidirectional Flow

```
Chat discovers new positions → Added to Polis as seed statements
Polis voting reveals cluster structure → VoteBot uses in responses
Polis voters cover positions chat users missed → Fills sparse vectors
Chat users raise concerns Polis didn't seed → Expands the landscape
```

The two modalities are genuinely complementary: Polis provides breadth (every voter covers all positions), chat provides depth (nuanced, contextualized opinions with reasoning).

### Questions to Consider

22. **Should VoteBot proactively ask about positions the user hasn't covered?** "You've shared your views on funding and timeline, but not teacher certification. Want to weigh in?" This improves vector density but could feel pushy.

23. **How do you prevent the LLM from steering opinions?** If VoteBot says "most people agree with position X" before the user has expressed a view, that's anchoring. Show cluster data only AFTER the user has spoken, or only when explicitly asked.

24. **What does the mobile experience look like?** Chat widget on mobile is already tight. Embedding Polis visualization alongside it is impractical. For mobile: chat-only with text-based cluster summaries, link to full Polis on desktop.

---

## Phase 8: Future — Legislative Drafting Pipeline (Not Yet Specified)

> **Status:** Conceptual roadmap only. This phase depends on the opinion pipeline (Phases 1-7) being operational and on identifying appropriate tools for AI-assisted legislative drafting. It is documented here to ensure upstream design decisions support this eventual goal.

### The Gap

Phases 1-7 produce: "Here's what verified voters think about this bill, organized by topic, with consensus and fault lines identified."

The missing step: "Here's draft legislative text that reflects what they want."

### Conceptual Pipeline

```
Input: Clustering results + Opinion landscape + Bill text
    |
    v
Step 1: Consensus → Policy Directives
    For each consensus position or strong majority stance:
    - Map to specific bill section(s) via PolicyPosition.bill_sections
    - Generate a concrete policy directive:
      "Amend Section 3(b)(2) to index the per-pupil funding formula to CPI-U"
    - Include evidence: voter count, agreement %, representative quotes
    |
    v
Step 2: Policy Directives → Legislative Text
    - Reference the existing bill text (already in RAG as bill-text documents)
    - Reference the relevant statutory code being amended
    - Generate draft amendment language following legislative conventions:
      amending clauses, cross-references, insertion/deletion formatting
    - Or generate a new bill section for novel claims not addressed by the original
    |
    v
Step 3: Community Review
    - Draft provisions become new Polis statements: "Do you support this amendment?"
    - Or VoteBot presents the draft: "Based on input from 312 voters, here's a
      proposed change to the funding formula. Does this capture what you meant?"
    - Feedback loop: revise draft based on community response
    |
    v
Output: Voter-informed draft amendment with provenance
    - Which voters contributed (anonymized, by district)
    - What consensus positions it reflects
    - Margin of support
    - Methodology link
```

### Why the Current Data Model Supports This

Design decisions made in Phases 1-7 that enable the drafting pipeline:

| Decision | How It Helps Drafting |
|---|---|
| `PolicyPosition.bill_sections` links stances to specific bill sections | Drafting tool knows exactly which sections to amend |
| `novel_claims` in OpinionSignal captures unaddressed voter concerns | Source material for new provisions, not just amendments |
| Opinion vectors in PostgreSQL with relational queries | "All confirmed stances on Section 3 from District 7 voters" is a SQL query |
| `verified_voters` table with district attribution | Draft can cite "63% of verified voters in CD-7 support this change" |
| Landscape version history preserved | Drafting tool can reference the exact position definitions voters responded to |
| Bill text already in RAG system (`bill-text` documents in Pinecone) | Drafting tool can retrieve the exact language being amended |
| Consent + provenance audit trail | Every opinion traces back to a signal, a conversation, and a verified voter |

### What's Needed (Not Yet Built)

1. **Legislative drafting LLM service** — Takes policy directives + bill text + statutory code → draft amendment language. This is a specialized capability; general LLMs can approximate it but may not produce legally valid text. Exploring tools in this space.

2. **Statutory code retrieval** — The RAG system has bill text but not the underlying statute being amended. For example, if HB 123 amends Florida Statute §1002.33, the drafting tool needs access to the current text of §1002.33. This may require a new data source (state legislative code databases).

3. **Amendment formatting conventions** — Each state and Congress have specific formatting rules for amendments (e.g., "On page 3, line 12, strike 'shall' and insert 'may'"). These conventions would need to be encoded in the drafting prompts or handled by a specialized tool.

4. **Legal review workflow** — AI-drafted legislative text needs human review before it can be presented as a serious proposal. The system should produce drafts for review, not final text.

### Questions for Future Exploration

32. What existing tools or services handle AI-assisted legislative drafting? (e.g., state legislative drafting offices, legal AI startups, academic projects)
33. Should the drafting output be a full amendment or a structured "policy brief" that a human drafter converts to legislative language?
34. How do you handle conflicts between majority opinion and legal/constitutional constraints? (Voters may want something that isn't legally permissible.)
35. What's the minimum participant count for a draft to carry democratic legitimacy? (Higher bar than opinion reporting.)

---

## Implementation Roadmap

### Milestone 1: Opinion Landscape Generation (Weeks 1-3)
- [ ] BillTopic and PolicyPosition extraction from bill text
- [ ] Position enrichment from org positions (Webflow CMS)
- [ ] Deduplication via embedding similarity
- [ ] Storage in Redis (cache) + PostgreSQL (durable)
- [ ] Integration with bill sync scheduler
- [ ] **Validation**: Generate landscapes for 10 real bills, human-evaluate quality

### Milestone 2: Guided Elicitation in VoteBot (Weeks 3-6)
- [ ] Landscape-aware system prompt injection
- [ ] Passive opinion extraction (post-response async)
- [ ] Position-matching: opinion -> PolicyPosition + stance score
- [ ] Novel claim detection and storage
- [ ] Incremental opinion vector construction per session
- [ ] Feature flag and config
- [ ] **Validation**: Run on 200 production logs, measure extraction accuracy

### Milestone 3: Polis Bridge (Weeks 6-8)
- [ ] Polis client service (create conversations, submit comments/votes)
- [ ] XID JWT generation
- [ ] Position -> Polis seed statement mapping
- [ ] Auto-create conversation on landscape generation
- [ ] Chat opinion -> Polis vote discretization and submission

### Milestone 4: Clustering Pipeline (Weeks 8-11)
- [ ] Unified matrix construction (Polis + chat)
- [ ] PCA + k-means implementation (or Delphi job type)
- [ ] Cluster labeling via LLM
- [ ] Results read from Delphi DynamoDB + cached in Redis
- [ ] Scheduled recomputation
- [ ] Minimum participant threshold logic

### Milestone 5: Consent Flow + Memberstack + Voter Verification (Weeks 11-16)
- [ ] Conversational consent prompt in chat (Stage 1)
- [ ] Edit/correct flow in natural language
- [ ] `services/memberstack.py` — Admin API client (create member, update metadata, validate token)
- [ ] Onboarding Step A: LLM tool extracts name + email → server creates Memberstack account via Admin API
- [ ] Server-side visitor-to-member linking (Redis, bidirectional)
- [ ] Opinion vector promotion (visitor_id → member_id durable storage)
- [ ] Onboarding Step B: LLM tool extracts DOB + zip → Catalist verification → update Memberstack profile
- [ ] Returning member detection (widget sends Memberstack JWT on load if already logged in)
- [ ] `create_voter_account` LLM tool definition for structured data extraction
- [ ] Vote submission to Polis on member confirmation
- [ ] Catalist Fusion Light API client (`services/catalist.py` — token management, verify_voter)
- [ ] Voter verification API endpoint (`POST /votebot/v1/voter/verify`)
- [ ] Catalist workflow setup (coordinate with Catalist account rep for field selection)
- [ ] EC2 Elastic IP + Catalist IP allowlisting
- [ ] DWID-based deduplication logic
- [ ] Verified voter badge + weight in clustering
- [ ] Multi-level district attribution (congressional, state senate, state house)
- [ ] Cluster summary in VoteBot responses
- [ ] "Where do I stand?" query handling

### Milestone 6: Emergent Positions + Full Loop (Weeks 14-18)
- [ ] Novel claim clustering pipeline
- [ ] Candidate position generation and quality gate
- [ ] Auto-injection into landscape + Polis
- [ ] Polis embed on DDP bill pages
- [ ] Cross-modality flow (chat <-> Polis)
- [ ] Production monitoring, A/B testing, iteration

---

## Opinion Data Storage Schema

This section defines the complete storage layout for all opinion-related data. Each record type has one authoritative storage location, a defined lifecycle, and explicit key structure.

### Storage Systems

| System | Role | Durability |
|---|---|---|
| **Redis** | Hot storage — active sessions, opinion vectors under construction, caches | Volatile (TTL-based). Data survives restarts if persistence is enabled, but not the system of record. |
| **PostgreSQL (RDS)** | Durable storage — finalized opinion vectors, landscapes, signals, verified voters, audit trail | Permanent. Source of truth for anything that survives beyond 90 days. Existing RDS instance shared with other DDP services. |
| **Polis PostgreSQL** | Vote storage — discretized votes submitted to Polis for math pipeline | Permanent. Polis math service reads from here. VoteBot writes, never modifies. Separate instance from DDP RDS. |
| **DynamoDB** | Delphi output — clustering results, narrative reports, topic labels | Permanent. Owned by Delphi (external tool). VoteBot reads as a consumer, does not write. |
| **Memberstack** | Member metadata — DWID, districts, verified status | Permanent. Synced from VoteBot on verification. |
| **JSONL logs** | Raw event trail — query_processed events contain extraction evidence | 90 days raw, then redacted (per analytics governance). Not queried at runtime. |

**Why PostgreSQL over DynamoDB for VoteBot's opinion data:** The opinion system will eventually need relational queries — joining opinion vectors with bill text, querying by district, aggregating across bills. PostgreSQL handles this natively. DDP already runs an RDS instance for other services. DynamoDB remains for Delphi's output (we're consumers, not owners of that infrastructure). A future legislative drafting service will also benefit from having opinion data, bill text references, and statutory code in the same relational database.

### Record 1: Opinion Landscape (per bill)

The set of topics and policy positions for a bill. Generated by LLM from bill text + org positions.

**Redis (hot cache):**
```
Key:    votebot:landscape:{bill_webflow_id}
Value:  JSON OpinionLandscape object
TTL:    None (refreshed on bill version change)

Key:    votebot:landscape:version:{bill_webflow_id}
Value:  Integer version counter
TTL:    None
```

**PostgreSQL (durable):**
```sql
CREATE TABLE opinion_landscapes (
    id SERIAL PRIMARY KEY,
    bill_webflow_id TEXT NOT NULL,
    version INT NOT NULL,
    bill_title TEXT,
    jurisdiction TEXT,
    landscape JSONB NOT NULL,          -- Full OpinionLandscape (topics, positions, metadata)
    total_positions INT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (bill_webflow_id, version)
);

CREATE INDEX idx_landscapes_bill ON opinion_landscapes (bill_webflow_id);
```

**Lifecycle:** Created on bill sync (Phase 1). Re-generated when `BillVersionSyncService` detects new bill text. Old versions retained in PostgreSQL for audit. Redis always holds the latest version.

### Record 2: Opinion Signal (per extraction event)

An individual extraction from a single chat message — the raw evidence that a user expressed a stance. Append-only, never modified.

**Redis (recent signals, for consent flow display):**
```
Key:    votebot:signals:{visitor_id}:{bill_webflow_id}
Value:  JSON array of OpinionSignal objects
TTL:    90 days (refreshed on each append)
```

**PostgreSQL (durable archive):**
```sql
CREATE TABLE opinion_signals (
    signal_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    visitor_id TEXT,
    member_id TEXT,                          -- Backfilled on account linking
    session_id TEXT NOT NULL,
    conversation_id TEXT,
    bill_webflow_id TEXT NOT NULL,
    jurisdiction TEXT,
    elicitation_mode TEXT NOT NULL,          -- 'passive', 'contextual', 'explicit', 'add_your_voice'
    position_stances JSONB NOT NULL,         -- [{position_id, stance, confidence, source}]
    novel_claims JSONB,                      -- [str]
    user_message TEXT,                       -- The exact message that produced this signal
    bot_response TEXT,                       -- VoteBot's response for context
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_signals_visitor_bill ON opinion_signals (visitor_id, bill_webflow_id);
CREATE INDEX idx_signals_member ON opinion_signals (member_id) WHERE member_id IS NOT NULL;
CREATE INDEX idx_signals_bill ON opinion_signals (bill_webflow_id);
```

**Lifecycle:** Created on every message that produces an extractable opinion (Phase 2). Immutable once written. The `member_id` column is backfilled when visitor → member linking occurs (`UPDATE opinion_signals SET member_id = ? WHERE visitor_id = ?`). Used by the consent flow to show "here's what I gathered." Used for extraction accuracy analysis. A future legislative drafting service can query signals by bill to find voter evidence for specific positions.

### Record 3: Opinion Vector (per visitor per bill)

The accumulated, current-state opinion vector — the single source of truth for what this person thinks about this bill. Built incrementally from Opinion Signals. This is what gets discretized and submitted to Polis.

**Redis (active vectors):**
```
Key:    votebot:opinions:{visitor_id}:{bill_webflow_id}
Value:  JSON object:
        {
          visitor_id: str,
          member_id: str | null,
          bill_webflow_id: str,
          landscape_version: int,       # Which landscape version these stances map to
          stances: {
            "hb123_funding_state_formula": {
              stance: +0.7,
              confidence: 0.85,
              source: "explicit_selection",
              last_updated: "2026-04-01T...",
              signal_id: "sig_abc123"   # Which signal produced this stance
            },
            "hb123_timeline_3yr": {
              stance: -0.8,
              confidence: 0.95,
              source: "contextual",
              last_updated: "2026-04-01T...",
              signal_id: "sig_def456"
            }
          },
          positions_covered: 8,         # Count of non-null stances
          consent_status: "none" | "pending" | "confirmed" | "declined",
          consent_timestamp: str | null,
          polis_submitted: bool,
          polis_last_submitted: str | null,
          created_at: str,
          updated_at: str
        }
TTL:    90 days for visitor_id-keyed (refreshed on any update)
        None for member_id-keyed (promoted on account creation)
```

**PostgreSQL (durable, for members):**
```sql
CREATE TABLE opinion_vectors (
    id SERIAL PRIMARY KEY,
    visitor_id TEXT,
    member_id TEXT,                          -- Set on account creation
    bill_webflow_id TEXT NOT NULL,
    landscape_version INT,
    stances JSONB NOT NULL DEFAULT '{}',     -- {position_id: {stance, confidence, source, ...}}
    positions_covered INT DEFAULT 0,
    consent_status TEXT DEFAULT 'none',       -- 'none', 'pending', 'confirmed', 'declined'
    consent_timestamp TIMESTAMPTZ,
    polis_submitted BOOLEAN DEFAULT FALSE,
    polis_last_submitted TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (visitor_id, bill_webflow_id),
    UNIQUE (member_id, bill_webflow_id)
);

CREATE INDEX idx_vectors_bill ON opinion_vectors (bill_webflow_id);
CREATE INDEX idx_vectors_member ON opinion_vectors (member_id) WHERE member_id IS NOT NULL;
CREATE INDEX idx_vectors_consent ON opinion_vectors (consent_status) WHERE consent_status = 'confirmed';
```

The relational structure enables queries a future drafting service needs:
- "All confirmed opinions on bill X" → `WHERE bill_webflow_id = ? AND consent_status = 'confirmed'`
- "All opinions from verified voters in District 7" → JOIN with `verified_voters` table
- "Consensus stances on bill X Section 3" → query stances JSONB for positions linked to Section 3

**Lifecycle:**
- **Created** when the first Opinion Signal for this visitor+bill is processed
- **Updated** when subsequent signals arrive. Conflict resolution: most recent stance wins; `explicit_selection` beats `inferred` at any timestamp
- **Promoted** from Redis (visitor_id key, 90-day TTL) to PostgreSQL (member_id key, permanent) when user creates Memberstack account
- **Re-keyed** on member linking: Redis key changes from `votebot:opinions:{visitor_id}:*` to `votebot:opinions:{member_id}:*`; PostgreSQL record created/updated with member_id
- **Polis submission** occurs when `consent_status` flips to `confirmed`: stances discretized (+1/-1/0 at ±0.3 threshold), submitted as votes via Polis API

### Record 4: Polis Conversation Mapping (per bill)

Maps bills to their Polis conversation and policy positions to Polis statement IDs.

**Redis:**
```
Key:    votebot:polis:bill:{bill_webflow_id}
Value:  Polis conversation_id (e.g., "bill_FL_HB123")
TTL:    None

Key:    votebot:polis:positions:{bill_webflow_id}
Value:  JSON dict { position_id: polis_tid }
        e.g., { "hb123_funding_state_formula": 5, "hb123_timeline_3yr": 12 }
TTL:    None
```

**Lifecycle:** Created when Polis conversation is set up (Phase 3). Updated when emergent positions are added as new seed statements.

### Record 5: Polis Votes (submitted to Polis PostgreSQL)

Discretized opinion stances submitted to Polis for the math pipeline. VoteBot writes these; Polis math service reads them.

**Polis PostgreSQL:**
```
Table:  votes (append-only history)
        votes_latest_unique (current state, upserted via DB rule)

Columns:
  zid:              int     # Polis conversation ID (= bill)
  pid:              int     # Polis participant ID (mapped from XID = "votebot_{member_id}")
  tid:              int     # Polis statement ID (= policy position, from polis:positions mapping)
  vote:             int     # +1 (agree), -1 (disagree), 0 (pass)
  weight_x_32767:   int     # Source confidence encoded as SMALLINT

Weight encoding:
  Polis direct vote:              32767 (1.0)
  Chat: confirmed via consent:    27851 (0.85)
  Chat: explicit selection:       27851 (0.85)
  Chat: contextual response:      22936 (0.7)
  Chat: passive inference:        16383 (0.5)  — only if included (see question 13)
```

**Lifecycle:** Written when a confirmed opinion vector is submitted to Polis. Updated (via upsert rule on `votes_latest_unique`) when a user revises a stance. The math service polls `votes` every 1 second and recomputes PCA/clusters.

### Record 6: Clustering Results (per bill)

Output of PCA + k-means + representativeness analysis.

**Polis PostgreSQL (Polis math output):**
```
Table:  math_main
PK:     (zid, math_env)
Value:  JSONB blob containing PCA components, cluster assignments,
        representativeness scores, consensus, group-votes
        (see PLAN-polis-math-pipeline.md for full schema)
```

**DynamoDB (Delphi enhanced results):**
```
Table:  Delphi_PCAResults
        Delphi_CommentClustersLLMTopicNames
        Delphi_NarrativeReports
        Delphi_CommentHierarchicalClusterAssignments
```

**Redis (VoteBot cache for chat responses):**
```
Key:    votebot:clusters:{bill_webflow_id}
Value:  JSON ClusteringResult object (simplified for VoteBot consumption):
        {
          bill_webflow_id: str,
          total_participants: int,
          n_clusters: int,
          clusters: [
            {
              cluster_id: int,
              size: int,
              percentage: float,
              label: str,
              defining_positions: [{ position_id, stance_direction, agreement_pct }],
              representative_quotes: [str]
            }
          ],
          consensus_positions: [position_id],
          divisive_positions: [position_id],
          last_computed: str,
          math_tick: int
        }
TTL:    1 hour (refreshed from Polis/Delphi on access if stale)
```

**Lifecycle:** Recomputed daily (05:00 UTC), threshold-triggered (20+ new confirmed opinions), or on-demand. VoteBot reads from Redis cache; cache miss triggers a fetch from Polis `math_main` + Delphi DynamoDB tables, merged into the simplified format above. VoteBot is a **consumer** of Delphi's DynamoDB output — it reads but never writes to Delphi tables.

### Record 7: Member Verification (per member)

Catalist DWID and district assignments for verified voters.

**Redis:**
```
Key:    votebot:member:{member_id}
Value:  JSON object:
        {
          member_id: str,
          visitor_ids: [str],           # All linked visitor_ids
          catalist_dwid: str | null,
          verified_voter: bool,
          jurisdiction: str | null,
          congressional_district: str | null,
          state_senate_district: str | null,
          state_house_district: str | null,
          verified_at: str | null,
          created_at: str
        }
TTL:    None (permanent for authenticated members)
```

**Memberstack (canonical for verification data):**
```
Custom fields on member profile:
  catalist_dwid: str
  verified_voter: bool
  jurisdiction: str
  congressional_district: str
  state_senate_district: str
  state_house_district: str
```

**PostgreSQL (for DWID-based dedup queries):**
```sql
CREATE TABLE verified_voters (
    id SERIAL PRIMARY KEY,
    catalist_dwid TEXT NOT NULL,
    member_id TEXT NOT NULL,
    jurisdiction TEXT,
    congressional_district TEXT,
    state_senate_district TEXT,
    state_house_district TEXT,
    registration_status TEXT,
    verified_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (catalist_dwid),                  -- One DWID = one verified voter
    UNIQUE (member_id)                       -- One member = one verification
);

CREATE INDEX idx_verified_jurisdiction ON verified_voters (jurisdiction);
CREATE INDEX idx_verified_district ON verified_voters (congressional_district);
```

Purpose: If two `member_id`s resolve to the same DWID, the unique constraint catches it. Query by district for aggregated opinion reports. A future legislative drafting service can join `verified_voters` with `opinion_vectors` to answer "what do verified voters in District 7 want changed about Section 3?"

**Lifecycle:** Created on Catalist verification (Step B of conversational onboarding). DWID and districts written to Redis, Memberstack, and PostgreSQL simultaneously. Updated if user re-verifies (e.g., after moving).

### Record 8: Visitor-Member Links

Bidirectional mapping between anonymous visitor IDs and authenticated member IDs.

**Redis:**
```
Key:    votebot:visitor_to_member:{visitor_id}
Value:  member_id
TTL:    None

Key:    votebot:member_to_visitors:{member_id}
Value:  JSON array of visitor_ids
TTL:    None
```

**Lifecycle:** Created on Memberstack account creation (Step A of conversational onboarding). A single member can have multiple visitor_ids (different browsers/devices). Used to merge anonymous activity into authenticated profiles and to re-key opinion vectors.

### Data Lifecycle Summary

```
Message arrives
    |
    v
[Analytics event logging]                    → JSONL (90 days, then redacted)
    |
    v
[Opinion extraction]                         → OpinionSignal → Redis (90d) + PostgreSQL (permanent)
    |
    v
[Opinion vector update]                      → OpinionVector → Redis (90d for visitors)
    |
    v
[Consent confirmed]
    |
    ├─ No account → stances stored against visitor_id (Redis, 90d TTL)
    |
    ├─ Account created (Step A) → vector promoted to member_id key
    |   → Redis (permanent) + PostgreSQL (permanent)
    |   → Polis votes submitted
    |
    └─ Voter verified (Step B) → DWID + districts added
        → Redis + Memberstack + PostgreSQL
        → Opinions carry 1.0x weight
```

### What Is NOT Stored

- **Raw PII from voter verification** (name, DOB, address) — used in transit for Catalist API call, then discarded
- **Chat messages in opinion storage** — the `user_message` field in OpinionSignal is the extraction evidence, stored separately from the opinion vector. The vector itself contains only position_id → stance mappings.
- **Polis vote history in VoteBot** — VoteBot writes to Polis but does not read back vote history. The Polis math pipeline is the authority on clustering; VoteBot reads only the aggregated results.
- **Direct Polis voter opinions in VoteBot's Redis** — Users who vote directly in Polis are not reflected in VoteBot's opinion vectors. They appear in clustering results (read from `math_main`) but not in `votebot:opinions:*` keys. The unified matrix is constructed by the Polis math service from `votes_latest_unique`, which includes both sources.

---

## Technical Considerations

### Deployment

- VoteBot (EC2, 2 uvicorn workers) handles chat, extraction, and elicitation
- Polis (Docker Compose, separate EC2 recommended) handles direct voting + math
- Delphi handles clustering jobs (existing worker infrastructure)
- DDP-API proxy routes to both services

### Cost Estimates

| Component | Per-unit | Monthly (750 queries/day) |
|-----------|----------|---------------------------|
| Opinion extraction (Haiku/4o-mini) | ~$0.002/msg | ~$45 |
| Landscape generation per bill | ~$0.10/bill | ~$10 (100 bills) |
| Clustering per bill per run | CPU only | Delphi worker time |
| Polis infrastructure | Fixed | ~$50-100 (EC2) |
| Catalist Fusion Light API | Per Catalist agreement | Depends on volume tier (contact Catalist) |
| **Total incremental** | | **~$105-155/month + Catalist** |

### Data Privacy

- Raw chat messages stored temporarily (Redis, 24h TTL). Production query logs retain raw text for 90 days, then redact to structured fields only (per analytics logging governance policy).
- Opinion vectors are keyed by `visitor_id` (opaque UUID, not PII) — inherently pseudonymized
- Polis participants are pseudonymous (XID = `votebot_{member_id}` for members, `votebot_{visitor_id}` for anonymous)
- Representative quotes in cluster results require consent
- Novel claims used for emergent positions are aggregated (no individual attribution)
- `visitor_id` is localStorage-based — users can reset their identity by clearing browser storage
- **Voter verification PII (name, DOB, address) is NOT stored** — submitted to Catalist API in transit, then discarded. Only the Catalist DWID (opaque person ID) + district assignments are retained.
- Catalist API credentials (`client_secret`) must be stored securely (env vars, not in code). Token cached in memory only.
- Catalist's own data handling is governed by their client agreement and data use policy.

### Failure Modes

| Failure | Impact | Mitigation |
|---------|--------|------------|
| Landscape generation produces bad positions | Poor elicitation | Human review of first 10 bills, iterate prompts |
| Opinion extraction misclassifies | Wrong vector entries | Confidence threshold, consent flow catches errors |
| Polis API down | Can't submit votes | Queue in Redis, retry with backoff |
| Clustering doesn't converge | No meaningful groups | Min participant threshold, degrade to per-position aggregates |
| User rejects extraction in consent flow | Lost data | Log rejection as training signal, thank user |
| LLM editorializes during elicitation | Biased opinions captured | Strong system prompt guardrails, neutral framing |
| Catalist match returns wrong person | Wrong DWID + district attribution | Catalist's matching is high-confidence (name + DOB + address); allow user to dispute and re-verify |
| Catalist match fails (real voter not found) | User can't verify | Common causes: recent move, name change, new registration. Allow retry with corrected info; accept unverified status gracefully |
| Catalist token expired mid-session | Verification call fails | Auto-refresh token (cache checks expiry with 5-min buffer); retry once on 403 |
| Catalist API down or slow | Verification unavailable | Return graceful error ("verification temporarily unavailable"); user continues as unverified member |
| Multi-account gaming (unverified) | Inflated opinion counts | Accept limitation for unverified; voter file ID deduplicates verified |
| Memberstack signup abandonment | User confirmed opinions but didn't create account | Opinions still stored against visitor_id (90-day TTL); prompt again on return |

---

## Open Design Questions (Summary)

### Product & UX
5. How aggressively should VoteBot elicit? (Only when asked vs. contextual prompting)
6. Should VoteBot show how many people hold each position before or after the user opines?
7. What if the user doesn't want to express opinions? (Invisible elicitation)
8. ~~Should opinion history persist across sessions? (Requires auth)~~ **Partially resolved** — `visitor_id` enables cross-session persistence on same browser without auth. Full cross-device persistence still requires auth.
18. When should the consent prompt appear?
19. In-chat consent or separate UI?
22. Should VoteBot proactively ask about uncovered positions?
23. How do you prevent LLM anchoring/steering?

### Data Model
1. How many total positions per bill? (Target: 25-50)
2. Should positions be mutually exclusive within a topic? (Recommend: no)
3. How to ensure positions are genuinely distinct? (Embedding similarity check)
4. Should "implement as written" be auto-generated for every topic?
13. Should passive (unconfirmed) chat opinions be included in clustering?
14. Minimum coverage threshold for inclusion in clustering?
17. How to handle emergent positions that split existing ones? (Versioning)

### Ethics & Privacy
19. In-chat consent or separate UI? → **Resolved**: in-chat consent, then Memberstack modal for account creation
20. ~~Can users revise after submitting?~~ → **Resolved**: yes, via `member_id` (cross-device) or `visitor_id` (same browser)
21. Is the passive participation tier ethically acceptable? → Recommendation: aggregate stats only, no individual clustering
24b. How long to retain raw voter verification data? → Only hashed voter file ID stored; raw data not retained
25. How to handle multi-account gaming? → Voter file ID deduplicates verified users; unverified accepted as limitation

### Architecture
10. Should Polis voters see topic groupings? (Requires Polis UI changes)
11. One conversation per bill or per topic?
12. Minimum participant count for meaningful clusters?
15. Should emergent positions require human approval?
16. Can emergent positions from one bill inform another? (Cross-bill learning)

### Voter Verification (Catalist)
26. ~~Which voter file provider for v1?~~ **Resolved** — Catalist Fusion Light API (national coverage, single API, DWID-based dedup)
27. ~~How to handle states with no public voter file API?~~ **Resolved** — Catalist covers all 50 states + DC
28. Should voter verification be one-time or periodic? (Recommendation: one-time, with optional re-verify for address/district updates. DWID persists across moves.)
29. What if a verified voter moves to a new district? (Re-verify updates district assignments from Catalist; DWID stays the same; old opinions retain their original district tag, new opinions get the new district)
30. What Catalist workflow fields should be included in the append? (Minimum: DWID, registration_status, congressional_district, state_senate_district, state_house_district. Optional: party_registration, demographics. Discuss with Catalist account rep during workflow setup.)
31. Should Catalist DWID be stored directly or hashed? (Recommendation: store directly — DWID is already an opaque identifier with no PII. Hashing adds complexity without privacy benefit since Catalist assigned the ID.)

---

## Risk Assessment and Mitigations

### Risk 1: Opinion Landscape Quality Is Untested (Critical)

**The risk:** Everything downstream — elicitation, vectors, clustering, reporting — depends on the LLM generating good topics and positions from bill text. If positions are vague, redundant, or miss what people actually care about, the entire system produces noise that looks like signal.

**Mitigations:**

1. **Pre-build validation gate (before any code).** Generate landscapes for 10 real bills across tracked jurisdictions using the prompts from Phase 1. Have 3 people independently evaluate each landscape on a rubric:
   - Topic coverage: Do these capture what people would actually argue about? (1-5)
   - Position distinctness: Could a voter distinguish between these options? (1-5)
   - Position completeness: Are obvious stances missing? (1-5)
   - Comfort: Would you feel comfortable choosing from these? (1-5)

   **Go/no-go threshold:** Average score >= 3.5 across all dimensions. If below, iterate prompts before building any infrastructure.

2. **Embedding similarity deduplication** (already in plan). Reject any position pair with cosine similarity > 0.85. But also add a **human-in-the-loop review** for the first 20 bills — automated dedup catches semantic duplicates, but not positions that are distinct-sounding yet functionally equivalent ("increase funding" vs. "allocate more resources").

3. **Org position anchoring.** For bills with existing organization support/oppose data in Webflow CMS, generate landscape positions that explicitly include the org-sourced stances. These are real advocacy positions held by real organizations — they anchor the landscape to reality rather than pure LLM generation.

4. **Iterative prompt refinement corpus.** Save every generated landscape (including rejected ones) with human evaluations. After 20 bills, use the good/bad examples as few-shot context in the generation prompt. The prompt improves with data.

5. **Fallback for bad landscapes.** If a bill's landscape scores below threshold and can't be improved, degrade gracefully: show per-position agree/disagree aggregates without clustering. Don't force bad positions into the elicitation flow.

### Risk 2: Passive Opinion Extraction Accuracy Is Unproven (High)

**The risk:** Inferring continuous stance scores (P1=+0.6) from natural language ("I think this bill needs way more funding") is a hard NLP task. Low accuracy means wrong vectors, which means wrong clusters, which means misleading reports.

**Mitigations:**

1. **Early accuracy benchmark (pull forward from Milestone 2).** Before building the extraction pipeline, run a manual test:
   - Take 50 real production chat messages from the existing query logs that contain opinion-like language
   - Generate a test landscape for the relevant bills
   - Have the LLM extract position stances
   - Have 2 humans independently score the same messages
   - Measure agreement (Cohen's kappa) between LLM extraction and human judgment

   **Go/no-go threshold:** Kappa >= 0.6 (moderate agreement) on binary stance direction (agree/disagree). If below, the extraction approach needs fundamental rethinking before building the pipeline.

2. **Confidence gating.** Every extraction gets a confidence score. Only stances with confidence >= 0.7 enter the opinion vector. Low-confidence extractions are logged for analysis but not acted on. This reduces noise at the cost of coverage.

3. **Consent flow as error correction.** The conversational consent step ("Here's what I gathered — does this sound right?") is the primary accuracy backstop. Track the **correction rate** — if users change >30% of extracted stances during consent, extraction accuracy is too low for passive mode.

4. **Separate accuracy tracking per elicitation mode.** Passive extraction (always-on) will have lower accuracy than explicit selection. Track and report accuracy by mode:
   - Explicit selection: expected >95% (user directly chose)
   - Contextual prompt response: expected >80% (user responded to options)
   - Passive inference: expected >60% (LLM extracted from unprompted text)

   If passive accuracy is below 60%, disable passive extraction and rely only on prompted modes.

5. **Progressive rollout.** Week 1-2: passive extraction only, no user-facing changes (silent data collection for accuracy measurement). Week 3-4: enable contextual prompting for 10% of sessions. Week 5+: expand based on measured accuracy.

### Risk 3: 7-Vote Minimum Creates a Participation Cliff (High)

**The risk:** Polis requires 7+ votes to include a participant in clustering. Casual chat users covering 2-3 topics are invisible to the analysis. The opinion map may be dominated by power users and Polis direct voters, undermining the "chat as primary input" value proposition.

**Mitigations:**

1. **"Add your voice" civic participation prompt (primary mitigation).** After VoteBot has passively extracted 3-4 stances, it knows which topics the user hasn't covered. It frames the remaining positions as civic participation — not a survey:

   ```
   VoteBot: "You've shared your views on funding and teacher
   certification. 234 people have weighed in on this bill so far.

   Want to quickly add your voice on two more topics? It takes
   about 30 seconds and helps build a more complete picture of
   what voters in your district think."

   User: "Sure"

   VoteBot: "On the implementation timeline — the bill proposes
   a 3-year rollout. Some people think that's enough, others
   want 5+ years for districts to adapt. Where do you fall?"

   User: "5 years for sure"

   VoteBot: "Got it. Last one — on enforcement: the bill creates
   a new oversight board. Some support that, others think existing
   agencies should handle it. Thoughts?"

   User: "I'd rather use existing agencies"

   VoteBot: "Thanks! Your views on 4 key topics have been recorded.
   Want to see how you compare to others?"
   ```

   This takes a user from 4 passively extracted stances to 8+ in two quick exchanges — comfortably above the 7-vote threshold. The design principles:

   - **Civic framing, not extractive.** "Add your voice" and "help build a complete picture" feel like participation, not data collection.
   - **Only triggers after demonstrated willingness.** 3+ passive stances means the user is already engaged and opinionated.
   - **Honest about what happens.** The user knows their input goes toward an opinion map.
   - **Sets time expectations.** "30 seconds" prevents fatigue anxiety.
   - **Covers uncovered topics specifically.** VoteBot selects positions the user hasn't addressed, maximizing new signal per exchange.
   - **Minimal exchanges needed.** 4 passive stances + 2 quick topic rounds (2 positions each) = 8 votes. Each topic round is a single question with clear options.

   **Implementation:** The agent tracks which `PolicyPosition` IDs have been matched for the current visitor. When count >= 3 and the user hasn't been prompted yet this session, inject the "add your voice" prompt after the next natural response. The prompt presents the 2-3 uncovered topics with the highest `comment-extremity` (most divisive = most valuable for clustering).

2. **Cross-session accumulation.** `visitor_id` and `member_id` persist across sessions. A user who discusses funding today and teacher certification next week accumulates votes. Over 2-3 visits, a moderately engaged user crosses the threshold without any single session feeling heavy.

3. **Guided elicitation mode (Mode 3) naturally exceeds threshold.** When a user says "help me figure out where I stand," the explicit elicitation walks through 3+ topics at 3-4 positions each — producing 10-15 votes, well above the minimum.

4. **Sub-threshold aggregate reporting.** Users below 7 votes are excluded from PCA/clustering but their individual votes still exist in Polis. A separate "aggregate only" view shows per-position agree/disagree percentages including all votes regardless of threshold. Casual participants still contribute to the overall picture.

5. **Threshold monitoring.** Track and report:
   - % of chat users above the 7-vote threshold (before and after "add your voice" prompt)
   - Prompt acceptance rate (what % of users who see "add your voice" say yes)
   - Average votes per chat user (per session and cumulative)
   - Time-to-threshold for returning users

   If <20% of chat users ever cross the threshold even with prompting, lower it in the Polis deployment (configurable in `conversation.clj`).

### Risk 4: Too Many Unresolved Product Decisions (Medium)

**The risk:** 31 open design questions, several of which are blocking. Building without resolving them means either rework or shipping something that doesn't match the product vision.

**Mitigations:**

1. **Prioritize the five blocking questions now.** These must be answered before implementation starts:

   | # | Question | Recommended Resolution |
   |---|----------|----------------------|
   | 5 | How aggressively should VoteBot elicit? | Contextual prompting when user shows interest in a topic; never push unprompted. Passive extraction always on but invisible. |
   | 13 | Should passive opinions enter clustering? | No — passive opinions contribute to aggregate counts only. Only confirmed stances (Active+) enter the vector space. |
   | 15 | Should emergent positions require human approval? | Auto-approve + admin notification for v1. Review queue with 48h auto-approve timeout. |
   | 21 | Is passive tier ethically acceptable? | Yes, if limited to aggregate stats ("312 discussions"). No individual clustering, no district attribution, no Polis submission without consent. |
   | 5/22 | Should VoteBot proactively ask about uncovered positions? | Only after user has voluntarily expressed 3+ opinions. Frame as "want to weigh in on more?" not "you haven't covered X yet." |

2. **Classify remaining questions by milestone.** Not all questions need answers before Milestone 1. Group them:
   - **Before Milestone 1:** Questions 1-4 (landscape design parameters)
   - **Before Milestone 2:** Questions 5, 7, 9 (elicitation behavior)
   - **Before Milestone 3:** Questions 10, 11 (Polis setup decisions)
   - **Before Milestone 5:** Questions 18, 19, 21 (consent flow UX)
   - **Can defer:** Questions 6, 8, 16, 17, 22, 23, 24 (optimization, not blocking)

3. **Decision log.** As each question is resolved, document the decision, the reasoning, and who made it — directly in this plan or in a linked decisions document. This prevents relitigating resolved questions later.

### Risk 5: Catalist Integration Has External Dependency Risk (Medium)

**The risk:** Catalist requires an agreement, workflow configuration, IP allowlisting, and credentials — all dependent on an external organization's timeline. If setup takes weeks, it blocks voter verification entirely.

**Mitigations:**

1. **Start the procurement process immediately — in parallel with Milestone 1.** Contact Catalist now to begin:
   - [ ] Initial outreach to Catalist account executive
   - [ ] Data use agreement review and signature
   - [ ] Workflow specification (input fields, output fields, matching strategy)
   - [ ] API credentials provisioning
   - [ ] EC2 Elastic IP assignment and allowlisting

   Target: credentials in hand before Milestone 5 begins (week 11).

2. **Design the verification flow to be Catalist-independent until Step B.** The conversational onboarding is already structured this way: Step A (name + email → Memberstack account) works without Catalist. Step B (DOB + zip → voter verification) requires Catalist. Ship Step A first; add Step B when Catalist access is ready.

3. **Build against a mock.** Create a `CatalistService` interface that can be backed by:
   - **Production:** Real Catalist Fusion Light API
   - **Development/testing:** Mock service that returns synthetic DWID + district data for a set of test personas

   This lets you build, test, and demo the full flow without waiting for credentials.

4. **Fallback if Catalist is delayed.** If the agreement stalls, voter verification can still work with a degraded path:
   - User self-reports state + district (unverified)
   - VoteBot uses the existing legislative calendar + OpenStates data to validate that the claimed district exists
   - Opinions carry "self-reported" weight (0.7x) instead of "verified" weight (1.0x)
   - Upgrade to Catalist-verified when access is available (re-verify, replace self-reported with DWID)

### Risk 6: No Governance for When the System Is Wrong (Medium)

**The risk:** If clustering produces a misleading narrative ("63% of your constituents oppose this"), there's no correction mechanism, no audit trail, and no methodology transparency. For a system that aspires to legislator accountability, this is a credibility gap.

**Mitigations:**

1. **Methodology page on the DDP website.** Publish a clear, non-technical explanation of how opinion maps are computed:
   - Where opinions come from (chat, Polis direct voting)
   - How opinions are weighted (verified voter > member > anonymous)
   - What clustering means (and doesn't mean)
   - Sample sizes and confidence levels
   - What "63% of verified voters" actually means (63% of the N people who participated, not 63% of all voters in the district)
   - Link to this from every opinion map and every VoteBot response that cites cluster data

2. **Confidence intervals and sample size on every claim.** Never show "63% oppose" without context:
   - "63% oppose (based on 47 verified voters in District 12, margin of error ±12%)"
   - Below N=30 verified voters: show aggregates only, no percentages
   - Below N=10: show "too few participants for reliable results"

   Define thresholds:
   | Verified voters in district | Display |
   |---|---|
   | < 10 | "A few voters have weighed in — not enough for a summary yet" |
   | 10-29 | Per-position aggregates only, no cluster narratives |
   | 30-99 | Full clusters with margin of error displayed |
   | 100+ | Full clusters, confident reporting |

3. **Audit trail.** Every opinion map version is immutable and timestamped:
   - `ClusteringResult.version` already exists in the schema
   - Store historical snapshots in PostgreSQL (retained for 1 year)
   - If a legislator disputes a claim, you can show exactly what data produced it
   - Log every recomputation trigger (daily schedule, threshold-triggered, admin-initiated)

4. **Challenge mechanism.** Add a "Report an issue" link on opinion map displays:
   - Routes to a review queue (could be a simple Slack notification initially)
   - Admin can re-run clustering, adjust parameters, or add a caveat to the display
   - Track challenges and resolutions in the audit log

5. **Limitations statement in VoteBot responses.** When VoteBot cites cluster data, include a brief caveat:
   - "Based on [N] participants who've shared their views — not a scientific survey."
   - Never claim representativeness of the full electorate
   - The LLM system prompt should include: "When citing opinion data, always mention the sample size and note this is not a representative survey."

6. **Pre-launch review.** Before publishing any district-level opinion data:
   - Legal review of claims about "constituent opinion" (especially if used in advocacy)
   - IRB-equivalent ethics review if partnering with academic institutions
   - Pilot with 2-3 friendly legislators who can give feedback on how they'd interpret the data

---

## Recommendation

**Start with Milestone 1** (landscape generation) as a standalone experiment. Generate opinion landscapes for 10 real bills across your tracked jurisdictions. Have 2-3 people evaluate:

1. Do the topics capture what people would actually debate?
2. Are the positions genuinely distinct and meaningful?
3. Is the range comprehensive — or are obvious positions missing?
4. Would you feel comfortable selecting from these options?

If the landscapes are good, **Milestone 2** (guided elicitation) is the product-defining step. Run it against production traffic with passive extraction only (no prompting) for 2 weeks, then enable contextual prompting for a subset of users. Measure:

- What % of messages contain extractable opinions?
- How many positions per session does a typical user cover?
- Do users engage with contextual prompts, or ignore them?
- What % of novel claims represent genuinely new positions?

The opinion landscape is the foundation everything builds on. If the positions are good, the elicitation works naturally, the vectors are meaningful, and the clustering produces real insight. If the positions are bad, nothing downstream can fix it. Invest the most iteration time here.

### Parallel Workstreams

While Milestone 1 is in progress, start these in parallel:

1. **Catalist procurement** — initial outreach, agreement review, workflow specification. Longest lead-time item. Target: credentials in hand by week 8.
2. **Extraction accuracy benchmark** — take 50 real production messages, test LLM extraction against human judgment. This validates the core assumption of Milestone 2 without building any infrastructure. Go/no-go: kappa >= 0.6.
3. **Resolve the 5 blocking product questions** (5, 13, 15, 21, 22) — see Risk 4 mitigations for recommended resolutions.
4. **Methodology page draft** — write the public-facing explanation of how opinion maps work. This forces clarity on what claims the system can and cannot make (see Risk 6).

### Go/No-Go Gates

| Gate | When | Criteria | If Fail |
|---|---|---|---|
| **Landscape quality** | After 10 bills evaluated | Average rubric score >= 3.5 | Iterate prompts; do not proceed to Milestone 2 |
| **Extraction accuracy** | After 50-message benchmark | Cohen's kappa >= 0.6 | Rethink extraction approach; consider explicit-only elicitation |
| **Chat user coverage** | After 2 weeks of passive extraction | >30% of chat users produce 3+ extractable stances per session | If below, contextual prompting is mandatory, not optional |
| **Cluster stability** | After 50+ participants on any bill | Clusters are reproducible across recomputation runs | If unstable, increase min participant threshold or simplify to per-position aggregates |
