# Jigsaw Stage B: Guided Elicitation

> Part of [Jigsaw Rollout Plan](./PLAN-jigsaw-overview.md)
>
> **Status:** Blocked on Stage A validation gates.

---

## 1. Goal

Show positions to engaged users and collect explicit selections. Still no Polis integration. This stage validates whether users will actually interact with the opinion system -- do they care enough to confirm, correct, and volunteer their stances?

---

## 2. Prerequisites (Stage A Validation Gates)

All of the following must be met before Stage B begins:

| Gate | Threshold |
|------|-----------|
| Landscape quality (human review) | >= 3.5 / 5 |
| Extraction inter-annotator kappa | >= 0.6 |
| Conversations with 3+ positions | > 10% of all conversations |

---

## 3. Elicitation Modes

Stage B introduces a graduated set of elicitation modes. Each mode is progressively more explicit. All modes coexist -- a single session may traverse several.

### Mode 1: Passive Extraction (always-on, carried forward from Stage A)

- [ ] Continues unchanged from Stage A
- [ ] Every bot response is analyzed for extractable user stances
- [ ] No user-visible behavior; extraction happens silently

### Mode 1.5: Lightweight Confirmation (surgical)

Fixes high-confidence extraction errors early. First 1-2 per session only; never blocks conversation flow.

- [ ] Trigger: passive extraction with confidence >= 0.65 AND not already confirmed
- [ ] Limit: max 2 per session, never back-to-back
- [ ] Never interrupts a multi-turn thread the user is driving
- [ ] User options: confirm (👍) or correct ("not exactly" → free text)
- [ ] Confirmation raises confidence to 0.9; correction replaces the stance

**Example dialog:**

> **User:** I think we need to protect small farms from being swallowed up by mega-corps, but I also don't want to kill innovation in ag-tech.
>
> **Bot:** That's a real tension. There are proposals that try to cap consolidation while keeping R&D incentives intact -- I can dig into the specifics if you want.
>
> _[Passive extraction: "supports limiting agricultural consolidation" -- confidence 0.55]_
>
> **Bot:** _(appended)_ By the way -- I picked up that you're leaning toward **limiting agricultural consolidation while preserving ag-tech innovation**. Did I get that right?
>
> **User:** Yeah, basically. Though I'd go further -- I think we need antitrust enforcement, not just caps.
>
> _[Stance updated: "supports antitrust enforcement against agricultural consolidation" -- confidence 0.9]_

### Mode 2: Contextual Prompting

Surface positions when the user shows interest in a topic. The bot presents the landscape of positions without advocating for any.

- [ ] Trigger: user asks about or discusses a topic covered by the landscape
- [ ] Present 2-3 relevant positions neutrally
- [ ] Let user self-select or articulate their own view
- [ ] Never present more than one contextual prompt per topic per session

**Example dialog:**

> **User:** What does SB-1234 do about housing density?
>
> **Bot:** SB-1234 would allow up to fourplex construction in single-family zones statewide. People have landed in a few different places on this:
>
> - **Support density broadly** -- more housing types everywhere reduces costs
> - **Support with local control** -- let cities decide where to upzone
> - **Oppose** -- neighborhood character and infrastructure can't handle it
>
> Where do you come down, or is it more nuanced than these?
>
> **User:** Somewhere between the first two. I want more housing but cities should get a say in design standards.
>
> _[Stance: "supports density with local design authority" -- confidence 0.85, source: contextual_prompt]_

### Mode 2.5: Structured Clarification

Resolves ambiguity or accelerates mapping when passive extraction can't confidently place the user. Presents options conversationally with a mandatory "something else" escape.

- [ ] Trigger: passive extraction confidence < 0.6, OR mixed/conflicting signals, OR user is unclear/hesitant
- [ ] Present 2-4 position options plus "something else" (free text) — **"something else" is mandatory, never omit it**
- [ ] Frame as exploration, not selection: "People tend to land in a few places on this — does one feel close, or is your view different?"
- [ ] Never present without conversational context (don't drop options cold)

**Example dialog:**

> **User:** I'm not sure about the enforcement part. It seems like a lot.
>
> _[Passive extraction: unclear — confidence 0.45, conflicting signals]_
>
> **Bot:** That's a common reaction. People tend to see enforcement a few ways — does one of these feel close, or is your view different?
>
> - **Support the new oversight board** — dedicated enforcement catches violations faster
> - **Use existing agencies** — another board adds bureaucracy without results
> - **Support enforcement but with sunset clause** — try it for 5 years, then evaluate
> - **Something else** _(tell me in your own words)_
>
> **User:** The sunset clause idea is interesting. I'd go with that.
>
> _[Stance: "supports enforcement with sunset clause" — confidence 0.85, source: structured_clarification]_

**Why this works:** Preserves user agency (always includes "something else"), improves mapping accuracy on ambiguous inputs, avoids forcing buckets. If the user picks "something else," their free-text response is treated as a novel claim candidate.

### Mode 3: Explicit Elicitation

User-initiated. Triggered by phrases like "help me figure out where I stand" or "what are the key issues?" The bot walks the user through topics.

- [ ] Trigger: explicit user request for opinion exploration
- [ ] Walk through 3-5 topics from the landscape
- [ ] Present positions for each, collect selections
- [ ] Summarize at the end

**Example dialog:**

> **User:** I haven't really thought through this bill. Can you help me figure out where I stand?
>
> **Bot:** Sure -- let's walk through the big questions in HB-567. There are about five key areas where people disagree. I'll lay out the main positions on each and you tell me where you land.
>
> **Topic 1: Funding source.** The bill proposes a new payroll tax. Here's where people fall:
>
> - **Support the payroll tax** -- broad base, predictable revenue
> - **Prefer income tax surcharge** -- more progressive
> - **Oppose any new tax** -- fund it through reallocation
>
> What feels closest to you?
>
> **User:** I'd prefer the income tax option.
>
> _[Stance recorded, move to Topic 2...]_

### "Add Your Voice" Civic Participation Prompt

After 2+ positions have been collected in a session (via any mode), prompt the user to round out their profile with a civic participation framing. Uses the same structured option format as Mode 2.5.

- [ ] Trigger: 2+ extracted positions AND positive engagement signal AND not already shown
- [ ] Prompt once per session, max
- [ ] Frame as civic participation, not data collection
- [ ] Target: 2+ additional topics to reach 7+ total positions

**Full example dialog:**

> _[System has passively extracted 4 stances on HB-567 during conversation]_
>
> **Bot:** Hey -- I noticed you've shared your perspective on a few parts of this bill already. You're actually close to having a full opinion profile on HB-567.
>
> If you weigh in on a couple more topics, your views can be part of how we show where the public stands on this legislation. No account needed, totally anonymous.
>
> Want to add your voice on a couple more?
>
> **User:** Sure, why not.
>
> **Bot:** Great. Here are two areas you haven't weighed in on yet:
>
> **Environmental review timelines.** The bill shortens CEQA review from 18 months to 9 months for qualifying projects.
>
> - **Support shorter timelines** -- current process delays critical projects
> - **Keep current timelines** -- environmental review catches real problems
> - **Shorten but add post-construction audits** -- speed up, but verify after
>
> Where do you land?
>
> **User:** Shorten but add audits. You can't just skip the review.
>
> **Bot:** Got it. One more:
>
> **Affordability mandates.** The bill requires 15% of units be below-market-rate in developments over 20 units.
>
> - **15% is about right** -- meaningful without killing feasibility
> - **Should be higher (25%+)** -- housing crisis demands more
> - **Oppose mandates** -- let the market work, subsidize separately
>
> **User:** Higher. 15% is a joke in this market.
>
> **Bot:** Noted. You've now weighed in on 6 topics across HB-567 -- thanks for adding your voice. These perspectives help paint an honest picture of where people stand.
>
> _[Opinion vector now has 6 stances; visitor flagged as "civic participant"]_

---

## 3b. Orchestration Layer

The orchestration layer is what makes the modes work together. It's the decision engine that determines which mode activates at each turn.

### Core Principle

> **Conversation drives → structure assists → never the reverse.**

Structured questions (Modes 2.5 and 3) are tools for fixing ambiguity and increasing density. They never drive the experience.

### Session State Tracking

```json
{
  "positions_extracted": 3,
  "positions_confirmed": 1,
  "avg_confidence": 0.68,
  "engagement_score": 0.72,
  "confirmations_used": 1,
  "structured_prompts_used": 0,
  "add_your_voice_shown": false
}
```

### Decision Engine (priority order, evaluated every turn)

1. **Always run Mode 1** (passive extraction) — on every user message
2. **If extraction is high-confidence (>= 0.65) and < 2 confirmations used** → consider Mode 1.5
3. **If ambiguity or low confidence (< 0.6)** → consider Mode 2.5 (structured clarification)
4. **If natural expansion possible (user discussing a topic)** → consider Mode 2 (contextual)
5. **If 2+ positions and positive engagement** → consider Mode 3 / "Add your voice"

### Hard Guardrails

These are non-negotiable. Violating any of these turns the system into a survey.

**Never do:**
- [ ] 2 structured prompts in a row (Modes 2.5 or 3)
- [ ] More than 3 total structured prompts per session
- [ ] Structured prompt without conversational context (don't drop options cold)
- [ ] Structured prompt at conversation start (wait for natural engagement)
- [ ] Present options without "something else" / free-text escape

**Always:**
- [ ] Allow skip on any prompt
- [ ] Allow free-text override on any structured question
- [ ] Prioritize answering the user's actual question over elicitation
- [ ] Stop immediately on signs of friction (short replies, topic changes, "I don't know")

### What to Measure (Orchestration Health)

| Metric | What It Tells You | Alarm Threshold |
|---|---|---|
| Structured prompt acceptance rate | Are users engaging with options? | < 20% = prompts are unwelcome |
| Free-text override rate ("something else") | Are your positions missing nuance? | > 40% = landscape needs improvement |
| Drop-off after structured prompt | Are you overusing them? | > 15% increase vs. baseline = too aggressive |
| Completion rate (≥3 positions) | North star metric | < 10% = system isn't working |

---

## 4. Full LLM-Generated Landscapes

Upgrade from Stage A's simplified landscapes to full LLM-generated versions.

- [ ] LLM generates 5-10 topics per bill, 3-6 positions per topic, sourced from bill text
- [ ] Enriched with organization positions pulled from Webflow CMS
- [ ] Deduplicated via embedding similarity -- reject position pairs with cosine similarity > 0.85
- [ ] Landscape-aware system prompt injection on bill pages (see Section 9)
- [ ] Intent taxonomy enforcement: both `primary_intent` and `sub_intent` must be drawn from central enums -- no taxonomy creep allowed

### Landscape Generation Pipeline

```
Bill text
  -> LLM topic extraction (5-10 topics)
  -> LLM position generation (3-6 per topic)
  -> Embedding generation (per position)
  -> Deduplication (cosine > 0.85 = reject)
  -> Org position enrichment (Webflow CMS)
  -> Landscape stored in PostgreSQL
  -> System prompt injection on bill pages
```

---

## 5. Opinion Vector Construction

Incremental opinion vectors keyed by `visitor_id` in Redis.

### Per-Stance Schema

| Field | Type | Description |
|-------|------|-------------|
| `position_id` | string | References the landscape position |
| `stance` | float | -1.0 (strongly oppose) to +1.0 (strongly support) |
| `confidence` | float | 0.0 to 1.0 |
| `source` | enum | `passive`, `inline_confirmation`, `contextual_prompt`, `structured_clarification`, `explicit_elicitation`, `add_your_voice` |
| `evidence_text` | string | User's exact words that produced this stance |
| `signal_id` | string | References the conversation signal |
| `last_updated` | timestamp | ISO 8601 |

### Conflict Resolution Rules

1. **Most recent wins** -- if a user changes their mind, the latest stance replaces the old one
2. **Explicit beats inferred at any timestamp** -- a stance from `explicit_elicitation` or `contextual_prompt` always overrides a `passive` stance, even if the passive extraction is more recent

### Redis Key Structure

```
opinion:{visitor_id}:{bill_webflow_id} -> Hash of position_id -> JSON stance object
```

---

## 6. Novel Claim Detection

Opinions that don't match any position in the current landscape are flagged as novel claims.

- [ ] After extraction, compute embedding similarity to all positions in the relevant topic
- [ ] If max cosine similarity < 0.5, flag as novel claim
- [ ] Store novel claims with evidence text, topic association, and visitor_id
- [ ] Novel claims are queued for Stage E emergent position discovery
- [ ] Do not discard -- these are signal, not noise

---

## 7. Position Lifecycle Management

### Landscape Version Tracking

- [ ] Every landscape is versioned (integer, monotonically increasing per bill)
- [ ] Opinion vectors reference the landscape version they were collected against
- [ ] Landscapes are immutable once published; updates create a new version

### Migration Strategies

| Event | Strategy |
|-------|----------|
| New position added | Null entry in existing vectors -- user has not weighed in |
| Position removed | Preserved in vector but excluded from aggregation |
| Position split | Map old stance to closest new position via embedding similarity |
| Position merged | Average stances from merged positions |

### `landscape_migrations` PostgreSQL Table

```sql
CREATE TABLE landscape_migrations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bill_webflow_id TEXT NOT NULL,
    from_version    INTEGER NOT NULL,
    to_version      INTEGER NOT NULL,
    changes         JSONB NOT NULL,       -- array of {type, position_ids, details}
    vectors_affected INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## 8. Extraction Auditability

### Evidence Storage

- [ ] Every stance stored with `evidence_text` -- the user's exact words that produced the extraction
- [ ] Evidence is never summarized or paraphrased; it is a verbatim quote from the conversation

### Admin Inspection Endpoint

- [ ] `GET /admin/opinions/:visitor_id/:bill_webflow_id` returns all stances with evidence text
- [ ] Response includes: position_id, stance, confidence, source, evidence_text, signal_id, timestamp

### Extraction Accuracy Dashboard

- [ ] Automated comparison: for users who provided both passive and explicit stances on the same position, measure agreement
- [ ] Track: agreement rate, average stance delta, correction direction distribution
- [ ] Surface in admin dashboard

### Extraction Audit Loop (non-optional)

- [ ] Daily: sample N extraction-evidence pairs
- [ ] Human reviewer rates: correct, partially correct, incorrect
- [ ] Track agreement rate over time
- [ ] Alert if accuracy drops below 70%
- [ ] This loop runs every day Stage B is active -- it is not optional

---

## 9. Elicitation Prompt System

When a bill page has a landscape, the following is injected into the system prompt.

### System Prompt Addition

```
You are assisting a user exploring legislation. A position landscape exists for this bill.

DO:
- Surface positions naturally when the user expresses interest in a relevant topic
- Present all positions neutrally and without ranking
- Accept nuance -- if the user's view doesn't match a listed position, acknowledge that
- Use the user's own language when reflecting their stance back
- Respect "I don't know" or "I haven't decided" as valid responses

DO NOT:
- Force the user to pick a position
- Present this as a survey or quiz
- Editorialize on which position is "better" or "more popular"
- Ask about more than one topic at a time
- Repeat a prompt the user has already declined or answered
- Treat silence as disagreement
```

---

## 10. Validation Gates (Must Pass Before Stage C)

| Gate | Threshold | Measurement |
|------|-----------|-------------|
| "Add your voice" acceptance rate | > 30% | Prompted users who engage / total prompted |
| Prompted users reaching 7+ positions | > 20% | Users with 7+ stances / prompted users who engaged |
| Extraction correction rate | < 30% | Stances changed during inline correction / total inline corrections |
| Position lifecycle integrity | >= 1 | At least one landscape update processed without data corruption |

All gates must pass before proceeding to Stage C.

---

## 11. Success Metrics

| Metric | Target |
|--------|--------|
| Inline confirmation acceptance (Mode 1.5) | > 50% of confirmations accepted or corrected (not ignored) |
| Contextual prompt acceptance (Mode 2) | > 30% of prompted users provide a stance |
| Structured clarification acceptance (Mode 2.5) | > 40% of prompted users select an option or provide free text |
| Free-text override rate ("something else") | < 40% (higher means landscape is missing positions) |
| "Add your voice" completion | > 20% of prompted users reach 7+ positions |
| Drop-off after structured prompt | < 15% increase vs. baseline session drop-off |
| Completion rate (≥3 positions per conversation) | > 10% of bill-page conversations (north star) |

---

## 12. New PostgreSQL Table

### `landscape_migrations`

See Section 7 for full schema. Summary:

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID | Primary key |
| `bill_webflow_id` | TEXT | Which bill |
| `from_version` | INTEGER | Previous landscape version |
| `to_version` | INTEGER | New landscape version |
| `changes` | JSONB | Array of change descriptors |
| `vectors_affected` | INTEGER | Count of opinion vectors migrated |
| `created_at` | TIMESTAMPTZ | When the migration ran |

---

## 13. What This Stage Does NOT Include

- **Memberstack accounts** -- no user accounts; everything keyed by anonymous visitor_id
- **Consent flow** -- no explicit consent collection (that's Stage C)
- **Polis integration** -- no data sent to Polis; opinion vectors stay local
- **Clustering** -- no group opinion analysis; individual vectors only
- **Voter verification** -- no identity verification of any kind
