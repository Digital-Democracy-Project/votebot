This is good. Really good. I’d call it mergeable with a short cleanup pass, not another redesign cycle.

It now reads like a plan written by someone who understands the difference between:
	•	operational logging
	•	analytics semantics
	•	product metrics
	•	governance

That was not true of the earliest drafts. This version is coherent, internally consistent, and actually implementable.  

What is now clearly strong

1) The metric model is finally disciplined

The section defining metrics by query / conversation / session / visitor level is one of the strongest parts of the doc now. That will prevent a lot of future dashboard nonsense where different aggregation levels get mixed together.  

2) Success tiers are framed correctly

Separating:
	•	system success
	•	citation-grounded success
	•	heuristic answer success
	•	user success

is exactly the right move. It prevents the team from telling itself comforting lies with one fake “success rate.”  

3) The event model is right-sized

Three event types is the correct v1 compromise:
	•	message_received
	•	query_processed
	•	conversation_ended

That is enough structure to be real, without turning the implementation into an analytics platform project.  

4) The conversation model is now practical

You tightened the boundary logic, documented the caveats, and added a conversation summary event. That is exactly what was missing before.  

5) The grounding model is much cleaner

Splitting:
	•	grounding_status
	•	external_augmentation

was the right fix. Those are orthogonal dimensions and should stay that way.  

6) Governance is now thoughtful, not hand-wavy

The retention/access section is honestly one of the most mature parts of the plan. Keeping structured fields while aging out raw text is a strong compromise.  

⸻

Remaining critiques

These are not “go back to the drawing board” issues. They’re mostly final-sharpening items.

1) message_received includes conversation_id, but boundary timing needs to be crystal clear

This is the biggest implementation subtlety left.

You say message_received is emitted when the server receives a user message, before processing. You also say conversation boundary detection happens on each message and may emit conversation_ended for the previous conversation before starting a new one.  

That means the sequencing matters a lot:
	1.	receive message
	2.	detect whether it starts a new conversation
	3.	maybe emit prior conversation_ended
	4.	assign new/current conversation_id
	5.	emit message_received

That’s probably what you intend, but I would state it explicitly, because otherwise someone could incorrectly emit message_received before boundary evaluation and attach the message to the wrong conversation.

I would add one sentence:

Conversation boundary evaluation occurs before message_received is emitted, so each message is logged against its final assigned conversation_id.

That would remove ambiguity.

⸻

2) entry_referrer living on query_processed is slightly odd

You correctly note that entry_referrer is session-entry context only and populated on the first message per session.  

That’s good. But I’d be careful about putting it on query_processed records as though it were a per-query field.

It’s not wrong, but semantically it’s really:
	•	a session-entry attribute
	•	optionally copied onto the first event(s)

Cleaner wording

Document it as:
	•	canonical at session-entry / first message_received
	•	may be null on later events by design

That avoids future confusion where someone thinks null means “unknown” rather than “not repeated.”

⸻

3) citation-grounded success is a good name, but still a little product-hostile

This is more naming than structure.

You define it as:
	•	retrieval_count > 0 and has_citations == true  

That’s a fair conservative proxy. But a PM or stakeholder may still hear “citation-grounded success” as “good answer rate.”

I’d keep the metric, but maybe label it even more cautiously in reports:
	•	citation_grounded_proxy_rate

Not necessary in the schema, but worth considering in presentation.

⸻

4) dominant_primary_intent needs a deterministic rule

You added it to conversation_ended, which is smart.  

But the plan does not say how it is determined.

Possible choices:
	•	first intent in conversation
	•	most frequent intent
	•	last intent
	•	highest-confidence intent
	•	priority-ordered intent

These can produce different answers.

Fix

Add one explicit rule, like:

dominant_primary_intent is the most frequent primary_intent observed in the conversation; ties break to the first seen intent.

That’s enough.

⸻

5) sub_intent heuristic drift could become messy quickly

The two-level taxonomy is good. But the line:

sub_intent uses a lightweight keyword match within the primary category  

is fine for v1, but it will become inconsistent unless you keep the taxonomy disciplined.

The risk is not implementation—it’s taxonomy creep:
	•	status
	•	explanation
	•	comparison
	•	positions
	•	bill_alignment

These are already a little uneven in granularity.

Not a blocker, but I’d strongly recommend that whoever implements this also creates:
	•	one central enum list
	•	one comment saying “do not add new sub_intents casually”

Otherwise six weeks from now you’ll have half-analytics, half-folksonomy.

⸻

6) retrieval_sources may need normalization

You derive it from document_type metadata. Good first step.  

But if your metadata is inconsistent, analytics will get noisy fast:
	•	bill-text
	•	bill_text
	•	bill
	•	organization
	•	org
	•	org-position

Recommendation

Add a note that retrieval_sources should be normalized to a controlled vocabulary before logging.

That’s small, but important.

⸻

7) conversation_ended on disconnect may blur abandonment vs normal tab close

You define "abandoned" as:
	•	session disconnected mid-conversation / WebSocket close while streaming or within a turn  

That’s reasonable, but “disconnect” is a noisy signal in the browser. A user may:
	•	close the tab after getting their answer
	•	background the page
	•	lose connectivity briefly

So abandoned is okay as an operational label, but I would be cautious not to interpret it as definite dissatisfaction.

I’d tweak wording

Call it:
	•	disconnect_mid_turn
or keep abandoned but add:

this is a transport-level proxy and may overstate true abandonment

That would protect against overinterpretation.

⸻

8) client_ip is still present in the schema, which is fine, but I’d explicitly mark it as non-analytic

Since it is broken now and infra-dependent later, you probably do not want teams building analytics around it. The plan already kicks the fix to a separate task.  

I’d just explicitly say:

client_ip is retained for debugging/infrastructure use, not for v1 product analytics.

Minor, but clarifying.

⸻

9) evaluate_production.py scope is now significant enough that I’d define what it is not

You smartly acknowledge it is becoming both evaluator and analytics engine.  

I would add one sentence:

It is a reporting layer over logs, not the source of truth.

That protects against logic drift where core definitions live partly in the logger and partly only in the report script.

⸻

10) One future metric is still not really a metric yet

You appropriately admit “conversion to vote” is not measurable in v1 and only proxied by responses containing a vote CTA link.  

That’s fine. I’d just be careful not to call the proxy “conversion” anywhere in reports. It’s not conversion; it’s:
	•	CTA offer rate
or
	•	vote CTA exposure rate

This is more about future discipline than the plan itself.

⸻

My recommended final edits before implementation

These are the only edits I’d actually push for:

Must tighten
	•	Clarify boundary evaluation happens before message_received logging
	•	Define deterministic rule for dominant_primary_intent
	•	Note that entry_referrer is session-entry context and null later by design

Strongly recommended
	•	Normalize retrieval_sources to a fixed vocabulary
	•	Add a caution that abandoned is a proxy, not proof of dissatisfaction
	•	Mark client_ip as debug-only for v1 analytics

Optional
	•	Be stricter in how citation-grounded success is described in reports
	•	Add a central controlled list for sub_intent

⸻

Bottom line

My honest assessment:

This is the first version I would feel comfortable implementing without further architecture debate.

It’s not perfect, but it’s now in the zone where the main risks are implementation bugs and taxonomy discipline, not conceptual weakness.

If you want the shortest verdict: approve with minor edits.