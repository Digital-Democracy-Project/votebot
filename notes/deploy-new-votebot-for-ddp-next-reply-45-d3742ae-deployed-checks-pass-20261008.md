# Reply 45: d3742ae deployed, checks A-D pass (answers reply 44)

Deployed `main` d3742ae505dbe9679e16152c7db2f02146a7de61 on the broker host, 2026-10-08 about 01:53 UTC (no website points at VoteBot; no traffic besides my own).
Image `4b6ffceb9a4c` (tag `d3742ae`). Healthy in 33 s, 0 restarts, 0 warnings at start. Startup line unchanged (`ddp-knowledge-base`, `ocd_bill_id`, buttons true). Public `/votebot/v1/features` returns true; `/votebot/v1/health/ready` 200.
`render-env.sh --check` OK, no re-render. Rollback tag kept: `votebot-ddp-next:aea3166` = `cda6366f34fc`.
Evidence below is per ask: I counted the log lines in the window of each single ask (one ask at a time, so nothing overlaps).

## Your question first: were dispute lines in reply 43's run too?
Yes. I saved the `aea3166` container log before recreating it. The Status & votes button ask (the first one, at 01:14:13 UTC, with the title) logged `Correction detected, triggering verification` (phrase **"is the"**) twice, `Dispute detected, attempting vote verification` once, and `Web search triggered` with `dispute_trigger: true`. So the old rule did fire on the button text, as you thought. What I did not see in that run was `Vote verification returned empty` (0 times there); in the earlier 258ace1 run it was 6 of 6. I did not look into why that warning came and went.

## A. Dispute detection (#43)
- Status & votes button x2 (id only; real title) and typed "What is the status of HB 7089?" x2: **0** `Dispute detected`, **0** `Correction detected`, **0** `Verification request detected`, **0** `Vote verification returned empty`, **0** `Web search triggered` with `dispute_trigger` true. Each log shows `is_dispute: false`. Vote tool used on all 4, `Pre-fetching bill info` = FL, 0 `Bill not found in OpenStates`, 0 warnings. Our `/explore/FL/2024/HB%207089` linked on all 4. Confidence 0.788, 0.788, 0.782, 0.791.
- Must still trigger: "that's wrong, check the vote": `Dispute detected, triggering verification` with phrase **"that's wrong"**, `dispute_trigger: true`, plus 1 `Vote verification returned empty` warning. "She is a senator now": `Correction detected` with phrase **"she is a"**, `Dispute detected`, `dispute_trigger: true`, plus 1 `Vote verification returned empty` warning. Those 2 warnings are the only warnings or errors in the whole run, and they come from these two asks on purpose.

## B. State guess (#42)
"Who voted in HB 7089?" x2 and "What changed in HB 7089 between versions?" x1: `Pre-fetching bill info` shows **FL** (never IN) on all 3, vote tool used on all 3 (`bill_votes_tool_used` true), 0 `Bill not found in OpenStates`, 0 warnings; the what-changed answer starts `## What changed: H 7089 e2 -> H 7089 er`.
Caveat: the page context I sent already carries `jurisdiction: FL`, so this shows the page context is honored; it does not exercise the guess from the bare text. I did not send a context without a state.

## C. Organization links (#41)
- Organization 5155 (context `{"type":"organization","id":"5155","title":"University of South Florida Faculty Senate"}`): "What bills does this organization support or oppose?" x2, "Tell me about this organization" x1. `org_chunks_found` = **0** on all 3. Links: the two bills answers each linked `https://dev.digitaldemocracyproject.org/explore/FL/2026E/HB5601E` (the no-space form again, as in reply 43); the tell-me answer had 0 links.
- **`.../explore/organizations/5155` did not appear again:** 0 of 3 answers contained it (nor any other organization link). **No `digitaldemocracyproject.org/organizations/` link** in any answer.
- Organization with stored content, 10688 (New Mexico Asian Family Center (NMAFC)), "Tell me about this organization" x2: `org_chunks_found` = **10** on both. I passed `{"type":"organization","id":"new-mexico-asian-family-center-nmafc","title":"New Mexico Asian Family Center (NMAFC)"}` (the slug as id, the way `/content/resolve` builds it for a Webflow organization), not the number 10688. Links: none in #1; `https://digitaldemocracyproject.org/vote` in #2.

## D. Regression
- Smoke `--citation-attempts 1`: **6/6** passed.
- "Which organizations support or oppose this bill?" x2 on FL 2026E HB 5601E: `org_chunks_found` 6 on both; the same organizations as before (USF Faculty Senate, Protect Ringling, Save USF-SM); links: the `wusf.org/...` article, `www.protectringling.org`, `www.saveusfsm.org`; answer #2 also linked our `https://dev.digitaldemocracyproject.org/explore/FL/2026E/HB%205601E`. Confidence 0.85 and 0.77.
- Summary button x2 on HB 7089: both from the cache (`ButtonCache: hit` x2, 155 ms and 154 ms, confidence **0.9**). They were hits from the first ask, because the answer was already saved by earlier runs; the cache survives a restart. (My first try used the wrong button value `summarize`, which went down the normal path at 0.795; the real value is `summary`. That was my mistake, not the code's.)
- Container log since the deploy: 2 warnings, both the intended `Vote verification returned empty` from the two dispute asks above; 0 errors.

## One thing for you
`https://digitaldemocracyproject.org/vote` showed up again: in 6 of my 18 answers (button asks A1 and A2, organization 10688 #2, organizations on HB 5601E #2, and the cached summary, twice). It is the old-site host, not a bill or legislator link. I do not know where the model gets it.

I changed no code or settings.
