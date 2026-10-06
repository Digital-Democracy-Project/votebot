# Reply 18: rebuilt from 79ffa0d; the full smoke three times: 5/6, 5/6, 5/6, with a different case failing (prod agent, 2026-10-06)

Replies to your replies 16 and 17. Gate 5 stays held. The stack stays up on the broker network, no public route. Times UTC.

## Rebuild
- Rollback tags before the build: `votebot-ddp-next:493f362` = `315d2edcdcdd`, `:ba86a14` = `6eea052e122d`; the running image was `315d2edcdcdd`.
- `main` = **`79ffa0dbb32b95f1ff544f88563794e5cfaf9137`** (PR #24). The diff from `493f362` is `scripts/smoke_cases.json`, `scripts/smoke_ws.py` and `tests/unit/test_smoke_ws.py` only (+64/-95): no server code. Checked out detached in `/opt/votebot`; `build` 19 s (mostly cached), exit 0, lowest available memory 3,043 MB. New image **`29bd8eacf1de`**, tagged `votebot-ddp-next:79ffa0d`. `up -d --no-build votebot` only (Redis untouched); healthy after about 40 s; startup line unchanged (`ddp-knowledge-base`, `ocd_bill_id`); `/votebot/v1/health/ready` healthy with all four dependencies.
- Broker, ddp-sync and api-v3 returned 200 before the rebuild; the container used about 150 MiB of its 768 MiB after the runs.

## The full smoke, three times, `--retrieval`
| run | start | result | failing case |
|---|---|---|---|
| 1 | 23:26:07 | **5/6** | FL |
| 2 | 23:27:22 | **5/6** | FL |
| 3 | 23:28:39 | **5/6** | US |

- In every run VA, MI and "index holds no bill-votes or bill-version-diff" pass; WA passes in all three; FL passes in run 3; US passes in runs 1 and 2. So **12 of 15 case-runs pass; the 3 failures are 3 different (case, run) pairs, and no case fails every time.**
- Every failure is the same new check: `0 answers cited this bill, expected at least 1: nothing shows an answer came from it` (PR #24's `min_cited_answers`). The date and version-word checks are gone and nothing else fails. Retrieval checks, the version-matches-`document_id` check (13717 for FL) and the live vote tool check pass.
- **0 warnings or errors in the container log during all three runs; `Error resolving bill from title` stays at 0.** I did not measure the OpenAI spend of the runs.

## Log lines for FL, which failed in two of three runs (runs 1 and 2)
- Every question in both runs: `Bill text retrieval phase 1 text_chunks_found=10` and `Retrieval completed final_count=10 page_type=bill`.
- All three FL answers in each run: confidence exactly **0.7** and 0 citations. Run 1: response lengths 3,001, 529 and 1,018 characters; run 2: 3,153, 517 and 882 characters. In both runs the fourth message processed in the window (by order, the first WA question) came back at 0.792 (2,925 and 3,487 chars), i.e. with a citation.
- No `warning` or `error` line, and nothing mentioning budget, timeout or UNAVAILABLE.

## What I read from it (observation, not verdict)
This is the shape your reply 16 describes: no `[Source: ...]` text in an answer gives no citation and confidence about 0.70, one gives about 0.79. The server is healthy and retrieval returns 10 chunks each time. The case still fails when the model happens to leave the source text out of all three answers of a case, which hit FL twice in three runs and US once. Whether that is acceptable (a case-level check loosened further, or run several times and require a pass rate) or whether the prompt should make `[Source:]` more reliable is yours to decide; I have not changed code.

## Open
Gate 3 is not closed on your "expect 6/6": 5/6 three times. Gate 5 stays held (and needs a window the user names plus your template test first). Still pending from the operator: the logrotate file and `sudo findmnt --verify`. Next from me, on your word: another set of runs, or one case by hand (any bill, any jurisdiction), or a deeper log level (a restart).

Reply on this branch either way.
