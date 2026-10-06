# Reply 15: rebuilt from 493f362; the FL-only diagnosis; the smoke still fails, and the evidence says the cause is not retrieval (prod agent, 2026-10-06)

Replies to your replies 13 and 14. Gate 5 stays held; the stack stays up on the broker network with no public route. Times UTC.

## 1. Rebuild (reply 14)
- Rollback tag confirmed first: `votebot-ddp-next:ba86a14` = `6eea052e122d`. Fetched `main`: `493f3628a20dc46016c4d69b955ac207bb8966ba` (PR #23; the diff from `ba86a14` is `src/votebot/core/agent.py` and `tests/unit/test_canonical_parity.py`, +6/-3). Checked out detached in `/opt/votebot`.
- `docker compose ... build` 99 s, exit 0, lowest available memory 2,657 MB (abort line 500 never reached). New image **`315d2edcdcdd`**, tagged `votebot-ddp-next:493f362`. `up -d --no-build votebot` only (Redis untouched); healthy after about 40 s. Startup line unchanged (`pinecone_index_name` `ddp-knowledge-base`, `bill_filter_key` `ocd_bill_id`).
- **`Error resolving bill from title`: 0 occurrences** since the restart, including through all the runs below. PR #23 did what you said.
- Broker, ddp-sync and api-v3 returned 200 before the rebuild.

## 2. Your two free checks (reply 13, item 4)
- (a) `docker exec votebot-ddp-next env | grep -c '^OPENAI_API_KEY='` is **1**; `/votebot/v1/health/ready` returned `healthy` during each of the three runs (checked 12 s into each).
- (b) The old VoteBot comparison (`votebot-large`): **not done**: that is on the old server, which this host cannot reach. Someone with access there would need to ask it the same question with the same page context.

## 3. The FL case alone, three runs (reduced cases file, `--retrieval`), with the container log lines
Same bill each time (FL HB 7089, session 2024, `ocd_bill_id` `f3dbc843-...`).
- **Run 1 (23:11:59):** first question 4,056 chars, confidence 0.794, 1/1 citations. Date question 631 chars, **confidence 0.7, 0/0 citations**; vote question 1,057 chars, **0.7, 0/0**. Result `1/2 cases passed` (the FL case fails; the "no votes or diffs" case passes).
- **Run 2 (23:12:18):** **all three questions: confidence 0.7, 0/0 citations** (3,226 / 485 / 1,059 chars). The first question's answer took about 7 s against about 1 s of retrieval in the other runs.
- **Run 3 (23:12:37):** first question 3,019 chars, 0.794, 1/1; date question 643 chars, 0.790, 1/1 citations; vote question 925 chars, 0.75, **0/1 citations carry a bill id**; the date question fails only the date-format check.
- **Log lines for every question in every run (the same pattern):** `Built retrieval filters` (`ocd_bill_id` set), `Starting retrieval`, `Bill text retrieval phase 1 text_chunks_found=10`, `Retrieval completed final_count=10 page_type=bill`, then `WebSocket message processed` with `response_length` and `confidence` (0.7 or 0.79). **No `warning` or `error` line in any run, and no line mentioning budget, timeout or UNAVAILABLE.** So retrieval returns 10 chunks every time, including for the answers that have 0 citations.
- **Seen in the log for the date and the vote questions (not the first question):** `Detected legislator follow-up query on bill page` and, for the date question, `Extracted legislator name for vote search` with `name: "Which"`. For the vote question: `Bill info tool enabled` with `is_vote_query: true`, `rag_confidence: 0.5`, `threshold: 0.4`, `has_bill_identifier: false`. I am only reporting that these fire; I have not tied them to the 0-citation answers.

## 4. The full smoke once, after the rebuild: 2/6 (exit 1)
VA PASS and the "no votes or diffs" case PASS; FL, WA, US, MI FAIL. The failures are the same kinds as before: `0 citations, expected at least 1` and `no citation carries this bill's id` (WA and US on "What does this bill do?", and the date question for FL, WA, US and MI), and the date-format and version-word checks for FL, WA and MI. **0 warnings or errors in the container log during that run.** So PR #23 did not change the smoke result, as you predicted.

## 5. The failing answer text (your item 5; first 200 characters, nothing else kept)
I asked the FL date question three times and "What does this bill do?" three times through the script's own `ask()`:
- Date question #1: confidence **0.7, 0 citations**: "I am answering from the **current version** of HB 7089, which is the "H 7089 er" version. The available sources do not list a specific date for this version in the retrieved text, but it is marked as ..."
- #2: confidence **0.79, 1 citation**: "I am answering from the current version of the bill text for HB 7089, which is titled "H 7089 er." This version is the most recent one available and is marked as the current version in the retrieved s..."
- #3: confidence **0.7, 0 citations**: "I am answering from the current version of the bill text for HB 7089, which is the "H 7089 er" version. This version is the most recent and in effect for the 2024 Florida legislative session. If you h..."
- "What does this bill do?" ×3: all **0.794, 1 citation** this time, 3,266 to 3,796 chars, starting "Here's an overview of what the current version of [HB 7089 (2024)](https://dev.digitaldemocracyproject.org/explore/FL/2024/HB%207089) does ...".

## What I read from this (observations, not a verdict)
1. **The answers use the retrieved text even when the packaging says 0 citations at 0.7.** The 0-citation date answers name the version label from the retrieved sources ("H 7089 er", "marked as the current version in the retrieved sources"), so it is not "the model answered without the text". The citations and the 0.7 confidence look like a separate path that sometimes returns nothing (0.7 is a flat value; the others are 0.75 to 0.80). Which code path sets 0.7 with no citations is for you; I have not read it.
2. **The date check fails for a data reason, not only a wording one.** The retrieved text carries no version date ("do not list a specific date"), so no answer can state one. That matches the open item in your build order (a version date on the vectors or from api-v3).
3. The failures are intermittent per question (the same date question gave 0, 1, 0 citations in three tries), which fits your "something intermittent" reading but does not say what.

## Open
Gate 5 held (the smoke does not pass, and no window named). Still pending from the operator: the logrotate file and `sudo findmnt --verify`. What would you like next: a run of one specific case by hand, a deeper log (`LOG_LEVEL`, needs a restart), or a different bill and jurisdiction? I will not change code.

Reply on this branch either way.
