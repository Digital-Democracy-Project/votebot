# Reply 34: PRs #29, #30 and #31 are merged. Deploy them and run the live checks below.

votebot `main` is now **6e0aaa960361b26fe648515d0c2fd202313328e0** (VOTEBOT-22 "what changed" names the versions, VOTEBOT-23 links to our own page, VOTEBOT-24 live lookup of a named legislator on every page). **Do reply 33 first** (switch `.env` to the `render-env.sh` output, one restart, check), **then this**, as a separate deploy so each can be rolled back on its own. Tag the running image first, build from this SHA, `up -d --no-build votebot`, record the image ID.

## Live checks (FL HB 7089 page context unless stated; session ids `check-N`; names and counts only, first 300 characters of an answer at most)
**A. "What changed" (VOTEBOT-22), x3:** "What changed between the first and the latest version of this bill?" Expect: the answer names the pair the log shows as read (`Live version diffs ... versions 1`, so `H 7089 e2` and `H 7089 er`, whatever the log says), and says that only that comparison was read / earlier steps were not compared. It must NOT say it compared "the first version" to the latest unless the pair read includes it. Report the log line and the first 300 characters of each answer.

**B. Links (VOTEBOT-23):** "How did the vote on this bill go?" x10 on the websocket. Expect **0 answers containing `openstates.org`** (in the text and in the citations' `url`/`document_id`); any link to the bill is our page (`/explore/FL/2024/HB%207089`). Then the **non-streaming** path, x3, with the REST endpoint `POST /votebot/v1/chat` (use the API key already in the container's environment from inside the container, never print it): the same expectation. This path was changed too. Report counts and any `openstates.org` hit with the first 200 characters around it.

**C. Named people on a general page (VOTEBOT-24)** (page context `{"type":"general"}`):
- "Who is Nancy Pelosi and which district does she represent?" x3: expect a `People lookup` log line each time, the answer naming CA-11 from the record, no `UNAVAILABLE` text, and no citations from the fallback (a general page pins nothing).
- "who is nancy pelosi" (lower case) x2: expect the same lookup.
- "Who is Nancy Pelsoi?" x3 (typo): expect the lookup to return nobody and the answer to say it could not find that name and ask for the exact name, **stating no district or role**. Report what the answer said.
- "Tell me about Jordan" x2: expect either one profile (if exactly one person matches) or no added context; it must not list several Jordans. Report which.
- "What is Medicaid expansion?" and "Summarize the Florida education budget" x2 each: expect **no `People lookup` line**.
- Count the `People lookup` lines and note any added latency (the `latency_ms` of a Pelosi answer vs a Medicaid answer).

**D. Regression:** "What does this bill do?" x10 on the bill page (expect 10/10 cited, own chunk only, as in reply 32); "thanks!" and "Hello" x3 each (0 cited); the Pelosi question on the legislator page context `ocd-person/8056c810-...` (unchanged: live lookup, 0 citations); the full smoke with `--citation-attempts 1` (expect 6/6). Container log: 0 warnings or errors expected.

## Rules
If any check fails, do not change code or the setting; report it with the evidence and leave the new image running (nothing is live for visitors). If the image does not come back healthy within about 2 minutes, roll back to the previous tag (runbook) and tell me. Reply on this branch.
