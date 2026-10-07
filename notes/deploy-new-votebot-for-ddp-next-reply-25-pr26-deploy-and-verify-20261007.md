# Reply 25: PR #26 (deterministic citations, VOTEBOT-21) is merged. Deploy it and run the live check.

Note: this is numbered 25 because a separate reply 24 (the dev agent's read-only questions to close VOTEBOT-14 and VOTEBOT-15) landed at the same time. They are independent. Do this one first (it changes the running image), then answer reply 24's questions: its VOTEBOT-15 live runs (items 5 to 7) are better run on the new image.

Thank you for replies 22 and 23. Gate 5 is done; nothing from you is outstanding there. This is a code change to VoteBot only.

## What changed
When the model writes no `[Source: ...]`, VoteBot now cites the retrieved chunks the answer was built from (`core/citations.py`): an answer must share at least 4 distinctive words with a chunk. Greetings, refusals, "I cannot find ...", and live-data answers (votes, legislators, positions) get none; a citation the model wrote is never replaced. Off-switch: `VOTEBOT_DETERMINISTIC_CITATIONS=false` in `/opt/votebot/.env` plus `up -d --force-recreate votebot`.

## Do
1. Pull `main` (now **2ababfff5f12a14e23a8328f8cda93f2a346cb56**). Tag the running image first (runbook: it is `votebot-ddp-next:a160e5c`), build, `up -d --no-build votebot`, check health. No settings change is needed (default on).
2. **Live check, because the thresholds were tuned on hand-written text, not real vectors.** From inside the container, so no key leaves the host:
   - Run the FL case of the smoke test alone three times (a reduced cases file with only FL, `--retrieval`, `--citation-attempts 1`). Report in each run how many of the three answers carried a citation. Expected: all three.
   - Send "What does this bill do?" ten times on the FL bill-page context (the same one the smoke test discovers; session id `citation-check-N`, via the websocket or the REST `/votebot/v1/chat` with the API key from the container's own env). Report how many of the ten had a citation, and for each: `confidence`, the number of citations, and whether `document_id` contains the bill's `ocd_bill_id`.
   - Send "thanks!" and "Hello" on the same bill page (3 times each). Expected: **no citations**.
   - Send one vote question ("How did the vote on this bill go?"): expected `bill_votes_tool_used` true and no citation from the fallback.
   - Look at the container log for `Cited retrieved chunks the model did not cite` (how often it fires) and any warning or error.
3. Report any answer that was substantive but uncited, or any "thanks" that got a citation, with the first 200 characters of the answer (nothing else) and the log line. If more than about 1 in 5 substantive answers is uncited, or any conversational reply is cited, set the off-switch and tell me; do not change code.
4. Do not change `--citation-attempts` or the smoke cases: I will put the smoke test back to one attempt only after your numbers.

Gate 5 and the other open items (logrotate, `findmnt --verify`, the render-env PR, the old-server freeze) are unchanged.
