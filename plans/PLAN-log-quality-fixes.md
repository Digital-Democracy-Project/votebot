# PLAN: Log Quality & Analytics Fixes

**Date:** 2026-03-29
**Status:** Deploy 1 COMPLETE (commit `5789b04`, 2026-03-29). Deploy 2 ACTIVE (toggle enabled 2026-03-30 03:46 UTC) — 48hr validation window ends 2026-04-01 ~04:00 UTC. Deploy 3 (frontend page_context) not started.

**Known observation:** With `--workers 2`, one uvicorn worker may serve stale code briefly after restart. During Deploy 2 validation, "can you make it more concise?" classified as `sub_intent: unknown` despite matching keywords locally — likely served by a worker that loaded before the restart fully propagated. Verify after 48 hours that sub-intent classification is consistent across all queries (no intermittent `unknown` for keywords that should match).

**Motivation:** Analysis of production logs from March 26–29 revealed four systemic issues degrading analytics quality: duplicate event logging, a confidence score floor effect, low citation rates, and poor sub-intent coverage. Together these make the logs unreliable for evaluating VoteBot's real-world performance.

**Data basis:** 807 log entries across 4 days (64 actual queries, 60 message_received, 683 conversation_ended — of which ~660 are duplicates).

**Deployment context:** VoteBot runs as a single uvicorn process per container on EC2 behind two domains (digitaldemocracyproject.org and votebot.digitaldemocracyproject.org). Session state is in-memory per process. WebSocket connections are sticky to one process, so in-memory state resets are safe. Redis is used for cross-instance coordination (Slack handoff routing, pub/sub) but not for conversation session state.

**Analytics consumers:** Confidence scores and citations are currently only used for manual performance evaluation (not consumed by dashboards or automated systems). This means changes to scoring formulas carry no downstream breakage risk.

**User-visible impact:** VoteBot displays citations as clickable links below every response. Any change to citation behavior directly affects what users see.

---

## Issue 1: Duplicate `conversation_ended` Events (Critical)

### Problem

A single conversation (`a4126868-70d:1`) emitted **258 `conversation_ended` events** across 3 days. On March 28, the entire log file (109 entries) consisted solely of duplicate `conversation_ended` events — zero actual queries. On March 29, 467 of 553 entries (84%) were conversation_ended, from only 17 unique conversations.

### Root Cause

**File:** `src/votebot/api/routes/websocket.py`, lines 524–535

When a WebSocket disconnects:

```python
except WebSocketDisconnect:
    session = manager.get_session(session_id)
    if session and session.get("conversation_message_counter", 0) > 0:
        session_copy = dict(session)
        if session_copy.get("conversation_has_response"):
            terminal = "inactive_end"
        else:
            terminal = "abandoned"
        await _emit_conversation_ended_with_state(session_id, session_copy, terminal)
    manager.disconnect(session_id)  # removes WS connection, preserves session
```

The disconnect handler emits `conversation_ended` if `conversation_message_counter > 0`, but **never resets that counter**. `manager.disconnect()` only removes the WebSocket connection — the session dict (with all its conversation state) persists in memory.

When the client reconnects (common with mobile browsers, tab backgrounding, network flaps), the session is restored (lines 504–511). The next disconnect finds `conversation_message_counter` still > 0 and emits another `conversation_ended` for the same conversation. This repeats indefinitely.

The growing `duration_seconds` values in the duplicates confirm this: `1027s → 1052s → 1100s → ... → 76716s` — the `conversation_start_time` is never reset, so duration keeps growing from the original start.

### Fix

After emitting `conversation_ended` on disconnect, reset the conversation state so reconnect cycles don't re-emit:

**File:** `src/votebot/api/routes/websocket.py`, lines 524–535

```python
except WebSocketDisconnect:
    session = manager.get_session(session_id)
    if session and session.get("conversation_message_counter", 0) > 0:
        session_copy = dict(session)
        if session_copy.get("conversation_has_response"):
            terminal = "inactive_end"
        else:
            terminal = "abandoned"
        await _emit_conversation_ended_with_state(session_id, session_copy, terminal)
        # Reset conversation state to prevent duplicate events on reconnect
        session["conversation_message_counter"] = 0
        session["conversation_has_response"] = False
        session["conversation_start_time"] = None
        session["conversation_intents"] = []
        session["conversation_had_handoff"] = False
        session["conversation_had_fallback"] = False
        session["conversation_had_retrieval_miss"] = False
    manager.disconnect(session_id)
```

Apply the same fix in the generic Exception handler (lines 536–541):

```python
except Exception as e:
    logger.exception("WebSocket error", session_id=session_id, error=str(e))
    session = manager.get_session(session_id)
    if session and session.get("conversation_message_counter", 0) > 0:
        await _emit_conversation_ended_with_state(session_id, dict(session), "abandoned")
        session["conversation_message_counter"] = 0
        session["conversation_has_response"] = False
        session["conversation_start_time"] = None
        session["conversation_intents"] = []
        session["conversation_had_handoff"] = False
        session["conversation_had_fallback"] = False
        session["conversation_had_retrieval_miss"] = False
    manager.disconnect(session_id)
```

**Note:** Do NOT increment `conversation_counter` here — that should only happen in `_start_new_conversation()` when the user actually sends a new message. We're just clearing the "has an active conversation" state.

### Testing

1. Unit test: simulate connect → send message → disconnect → reconnect → disconnect. Assert only one `conversation_ended` event is emitted.
2. Unit test: simulate connect → send message → disconnect → reconnect → send new message → disconnect. Assert two `conversation_ended` events (one per actual conversation).
3. Manual: deploy and verify 03-30 logs show 1:1 ratio of conversations to `conversation_ended` events.

---

## Issue 2: Confidence Floor at 0.70 (High)

### Problem

37 of 64 queries (58%) have a confidence of exactly `0.700`. This flat floor makes confidence useless for distinguishing response quality — a response with 10 retrieved chunks and no citations gets the same score as a response that cited 3 sources.

### Root Cause

**File:** `src/votebot/core/agent.py`, lines 872–924

The `_calculate_confidence()` formula:

```python
confidence = 0.5                          # base
if retrieval_count > 0:
    confidence += 0.2                     # → 0.7 for any retrieval
if citations:
    confidence += min(len(citations) * 0.05, 0.2)
if citations:
    avg_relevance = sum(c.relevance_score or 0 for c in citations) / len(citations)
    confidence += avg_relevance * 0.1
if web_search_used:
    confidence += 0.1
# uncertainty penalty: -0.15
```

**The problem is structural:** Any query with retrieval (which is nearly all of them) gets `0.5 + 0.2 = 0.7`. The only way above 0.7 is through citations, and citations require the LLM to include explicit `[Source: ...]` references that match retrieved chunks. When the LLM doesn't cite sources (which is ~58% of the time), confidence is stuck at 0.7.

The formula ignores the actual Pinecone relevance scores from retrieval — the `_calculate_rag_confidence()` method (lines 830–870) does use them, but its result is only used for deciding whether to trigger web search, not for the final logged confidence.

### Fix

Incorporate retrieval quality (Pinecone scores) into the final confidence calculation. The `_calculate_rag_confidence()` result is already available at the call site.

**File:** `src/votebot/core/agent.py`, modify `_calculate_confidence()`:

```python
def _calculate_confidence(
    self,
    response: str,
    retrieval_count: int,
    citations: list[Citation],
    web_search_used: bool = False,
    retrieval_result=None,  # NEW parameter
) -> float:
    confidence = 0.5

    # Graduated retrieval boost based on actual chunk scores (not just presence)
    if retrieval_result and retrieval_result.chunks:
        top_scores = [c.score for c in retrieval_result.chunks[:3] if c.score]
        if top_scores:
            avg_top = sum(top_scores) / len(top_scores)
            # Scale: avg_top of 0.3 → +0.10, avg_top of 0.5 → +0.17, avg_top of 0.8 → +0.27
            confidence += min(avg_top * 0.33, 0.3)
    elif retrieval_count > 0:
        # Fallback if retrieval_result not available
        confidence += 0.15

    # Citation boost (unchanged)
    if citations:
        confidence += min(len(citations) * 0.05, 0.2)
    if citations:
        avg_relevance = sum(c.relevance_score or 0 for c in citations) / len(citations)
        confidence += avg_relevance * 0.1

    if web_search_used:
        confidence += 0.1

    # Uncertainty penalty (unchanged)
    uncertainty_phrases = [
        "i'm not sure", "i don't know", "i cannot find",
        "no information", "unclear",
    ]
    response_lower = response.lower()
    for phrase in uncertainty_phrases:
        if phrase in response_lower:
            confidence -= 0.15
            break

    return max(0.0, min(1.0, confidence))
```

Then pass `retrieval_result` at the two call sites:
- Non-streaming: ~line 435
- Streaming: ~line 678

**Expected impact:** Confidence will now range from ~0.60 (low retrieval scores, no citations) to ~0.95 (high scores + multiple citations), with better discrimination between response qualities.

### Testing

1. Unit test: verify confidence varies meaningfully across different retrieval score distributions.
2. Check that the expected range produces sensible thresholds for any downstream consumers (e.g., the fallback trigger still works correctly).

---

## Issue 3: Low Citation Rate — 58% of Responses Ungrounded (High)

### Problem

37 of 64 queries have zero citations (`grounding_status: "partial"`). The bot retrieves relevant documents but the LLM response often doesn't include `[Source: ...]` references, so `_extract_citations()` finds nothing to match.

### Root Cause

**File:** `src/votebot/core/agent.py`, lines 761–828

Citation extraction relies on the LLM including explicit `[Source: name]` or `[Source: name](url)` patterns in its response text (lines 784–785). When the LLM answers conversationally without citing — common for follow-up questions like "can you make it more concise?" or "what's the latest status?" — no citations are extracted even though the response was informed by retrieved chunks.

This is especially prevalent for the **summary editing pattern** that dominates the logs (concise/expand/trim requests). The LLM is reformulating previously retrieved content without re-citing it.

### Fix: Prompt-Level Citation Encouragement (Only)

Since VoteBot displays citations as **clickable links below every response**, we must NOT add implicit/fallback citations. Attaching retrieval chunks the LLM didn't explicitly reference would show users sources that may not correspond to anything in the response text — effectively hallucinated citations. This was flagged in PM review as a high-severity concern.

**The fix is prompt-only:** reinforce citation instructions so the LLM cites more consistently, especially on follow-up turns.

**File:** `src/votebot/core/prompts.py` — add to the system prompt instructions:

```
When your response contains information from the provided context documents,
include source references using the format [Source: Name](URL). Always cite
sources when providing factual claims about bills, legislators, or organizations,
even in follow-up responses that rephrase earlier information. If you are
reformulating or condensing a previous answer, retain the original source
citations.
```

Review the existing prompt to ensure this doesn't conflict with current instructions.

**Runtime toggle:** Gate the new prompt addition behind an env var `VOTEBOT_ENHANCED_CITATION_PROMPT=true` (default `false`). This allows enabling/disabling without a redeploy.

**File:** `src/votebot/config.py` — add:
```python
enhanced_citation_prompt: bool = False  # env: VOTEBOT_ENHANCED_CITATION_PROMPT
```

**File:** `src/votebot/core/prompts.py` — conditionally include the citation reinforcement block only when the setting is enabled.

**What we're NOT doing:** No implicit/fallback citation matching. The `grounding_status: "partial"` metric in logs will continue to honestly reflect when the LLM didn't cite — this is a useful signal for prompt tuning, not something to paper over with synthetic citations.

**Log-only grounding context (optional follow-up):** If we later want to track retrieval-backed-but-uncited responses for analytics without showing them to users, we can add a separate log-only field (`retrieval_backing: bool`) that doesn't flow to the UI. This is deferred — not part of this plan.

### Acceptance Criteria

- Citation rate on bill summary queries rises from ~42% to >= 60% within 48 hours of enabling
- No increase in hallucinated or irrelevant citations (spot-check 20 responses manually)
- Response latency P90 does not increase by more than 2 seconds

### Testing

1. A/B comparison: run the same 10 queries with toggle off vs on, count citation rate.
2. Manual review: verify follow-up responses ("make it more concise") now retain citations from the initial response.
3. Verify no regressions in response quality or latency from the prompt addition.

---

## Issue 4: Sub-Intent Classification — 50% "unknown" (Medium)

### Problem

30 of 60 new-format queries have `sub_intent: "unknown"`. The keyword-based classifier in `intent.py` misses common query patterns because its keyword lists are too narrow.

### Root Cause

**File:** `src/votebot/utils/intent.py`, lines 110–147

The keyword maps have gaps for the most common real-world query patterns observed in logs:

| Query pattern | Expected sub_intent | Why it misses |
|---|---|---|
| "tell me about this bill" | summary | "tell me about" not in keywords |
| "can you expand this summary" | summary | "expand" not in keywords |
| "make it more concise" | summary | "concise" not in keywords |
| "pros and cons" | support_opposition | "pros", "cons" not in keywords |
| "what are the benefits" | support_opposition | "benefits" not in keywords |
| "who is sponsoring" | (bill context) status | "sponsoring" triggers legislator match but should be bill sub |
| "check your sources" | meta (or unknown) | legitimate unknown, but could be flagged |
| "help me construct an email" | (civic action) | no sub_intent for civic engagement |

The `_BILL_SUB_KEYWORDS["summary"]` list (line 119) has: `"summary", "summarize", "overview", "about", "what is this bill"`. It misses the entire family of rephrase/editing requests that dominate real usage: "expand", "concise", "trim", "paragraph format", "pros and cons", "benefits", "tell me about".

### Fix

Expand the keyword lists based on observed log patterns. Keep the lightweight regex/keyword approach (no ML) per the file's own guidance.

**File:** `src/votebot/utils/intent.py`

```python
_BILL_SUB_KEYWORDS: dict[str, list[str]] = {
    "vote_history": [
        "vote", "voted", "voting", "yea", "nay", "roll call", "tally",
        "passed the house", "passed the senate",
    ],
    "support_opposition": [
        "support", "oppose", "position", "stance", "for or against",
        "who supports", "who opposes", "backed", "endorses",
        "pros and cons", "pros", "cons", "benefits", "drawbacks",
        "arguments for", "arguments against", "advantages", "disadvantages",
    ],
    "status": [
        "status", "passed", "failed", "committee", "signed", "vetoed",
        "introduced", "referred", "latest action", "latest status",
        "what happened", "current status", "where is this bill",
        "sponsor", "sponsoring", "cosponsor", "cosponsoring",
        "amended", "amendment",
    ],
    "explanation": [
        "explain", "what does", "what is", "mean", "means",
        "rephrase", "simpler", "plain language",
        "help me understand", "break down", "what are these",
        "referenced in", "specific sections",
    ],
    "comparison": ["compare", "difference", "vs", "versus", "similar"],
    "summary": [
        "summary", "summarize", "overview", "about", "what is this bill",
        "tell me about", "expand", "concise", "trim", "shorten",
        "paragraph", "more detail", "less detail", "rewrite",
        "make it", "add back", "slightly more",
    ],
}

_OUT_OF_SCOPE_SUB_KEYWORDS: dict[str, list[str]] = {
    "greeting": ["hello", "hi ", "hey ", "good morning", "good afternoon"],
    "off_topic": ["weather", "recipe", "joke", "sports", "movie"],
    "meta": [
        "thanks", "thank you", "bye", "goodbye", "ok", "great",
        "check your sources", "recheck",
    ],
}
```

Also add a new bill sub_intent for civic engagement actions observed in logs:

```python
# In SubIntent enum:
CIVIC_ACTION = "civic_action"

# In _BILL_SUB_KEYWORDS:
"civic_action": [
    "email", "letter", "contact", "write to", "call my",
    "tell my representative", "tell my senator",
],
```

**Important:** Per the file's own header warning ("Do not add new intent values casually — taxonomy creep degrades analytics consistency"), the `civic_action` addition should be a deliberate decision. If you'd rather not add it, simply expand the existing `summary` and `support_opposition` buckets as shown above.

### Testing

1. Unit test: run the classifier against all 64 messages from the March 26–29 logs. Assert `unknown` rate drops below 20%.
2. Regression test: existing classified queries should not change sub_intent (check against a frozen fixture).

---

## Issue 5: Missing `page_context` Fields (Low)

### Problem

Most log entries have `page_context.id`, `page_context.jurisdiction`, and `page_context.webflow_id` as `null`. This is a frontend issue — the chat widget isn't passing all available context.

### Root Cause

**File:** `src/votebot/api/routes/websocket.py`, line 625

The server accepts whatever the client sends:
```python
page_context_data = payload.get("page_context", {"type": "general"})
```

The client widget is sending `type`, `title`, and `slug` but omitting `id`, `jurisdiction`, and `webflow_id`. These fields are available in the Webflow CMS page context and should be included.

### Fix

This is a frontend/chat-widget change. The widget needs to extract these fields from the Webflow page data attributes and include them in the `page_context` payload.

**File:** `chat-widget/` (or `chat-widget-poc/`) — wherever the WebSocket message is constructed.

The widget should send:
```json
{
  "type": "bill",
  "id": "HB 155",
  "title": "...",
  "jurisdiction": "FL",
  "webflow_id": "69c01c3c3411c4e0da5748b0",
  "slug": "..."
}
```

These values are typically available as `data-*` attributes on the Webflow page or can be extracted from the page URL structure.

**Note:** This is lower priority than the other fixes since retrieval still works via `slug` (which IS being sent). But having `jurisdiction` and `webflow_id` would improve retrieval filtering precision.

### Testing

1. Verify the chat widget sends complete page_context by inspecting WebSocket frames in browser DevTools.
2. Check that subsequent logs have non-null `id`, `jurisdiction`, and `webflow_id` fields.

---

## Deployment Strategy

### Deploy 1: Critical bug fix (Issues 1 + 2 + 4)

Ship together as a single backend release. These are all safe, low-risk changes:
- Issue 1 (duplicate conversation_ended): surgical state reset, immediate log quality improvement
- Issue 2 (confidence floor): scoring formula change, analytics-only impact (no downstream consumers)
- Issue 4 (sub-intent keywords): additive keyword expansion, no existing classifications change

**Acceptance criteria (24hr post-deploy):**
- `conversation_ended` events per unique conversation_id < 2 (target: exactly 1)
- Confidence score standard deviation > 0.05 (i.e. no longer a spike at 0.70)
- Sub-intent "unknown" rate < 20% (down from ~50%)

### Deploy 2: Prompt tuning (Issue 3)

Ship code in Deploy 1 but keep `VOTEBOT_ENHANCED_CITATION_PROMPT=false`. After Deploy 1 is validated:
1. Set `VOTEBOT_ENHANCED_CITATION_PROMPT=true` via env var (no redeploy needed)
2. Measure citation rate over 48 hours against Deploy 1 baseline
3. If citation rate doesn't reach >= 60% or quality degrades, set back to `false` and iterate on prompt wording

### Deploy 3: Frontend (Issue 5)

Separate chat widget deploy. Lower priority — retrieval works via `slug` which is already being sent. Ship when convenient.

### Rollback

Issues 1, 2, and 4 are all backward-compatible code changes. If any issue arises:
- Issue 1: revert the state reset lines; duplicates resume but no data loss
- Issue 2: revert `_calculate_confidence()` signature and body; scores return to 0.70 floor
- Issue 4: revert keyword lists; sub-intent returns to previous classification
- Issue 3: set `VOTEBOT_ENHANCED_CITATION_PROMPT=false` — instant rollback, no redeploy

No additional feature flags needed — issues 1/2/4 are small enough to revert via a single commit each, and the analytics consumer is manual (no automated systems to coordinate with). Issue 3 has its own env var toggle.

### Multi-Container Safety Note

The duplicate `conversation_ended` bug (Issue 1) is a **same-process** problem: the in-memory session dict persists after disconnect with stale conversation state. If a reconnect lands on a different container, the session dict won't exist there (sessions are in-memory, not in Redis), so `conversation_message_counter` will be 0 and no duplicate will be emitted. WebSocket connections are long-lived TCP connections pinned to one process for their lifetime; only reconnects create new connections. The fix correctly targets the only scenario that produces duplicates.

Sub-intent labels (Issue 4) are strictly offline analytics — they do not feed any automated routing, fallback logic, or user-facing behavior. The web search fallback uses `_calculate_rag_confidence()` (a separate method from Issue 2's `_calculate_confidence()`), so changes to the final confidence formula cannot trigger unintended fallback paths.
