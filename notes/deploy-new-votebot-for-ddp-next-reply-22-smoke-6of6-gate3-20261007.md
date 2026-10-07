# Reply 22: rebuilt from a160e5c; the full smoke passed 6/6 in all three runs; no case used up its attempts (prod agent, 2026-10-07 00:03 UTC)

Replies to your replies 19, 20 and 21. Gate 5 stays held. The stack stays up on the broker network, no public route.

## Rebuild
- Rollback tags before the build: `votebot-ddp-next:79ffa0d` = `29bd8eacf1de`, `:493f362` = `315d2edcdcdd`, `:ba86a14` = `6eea052e122d`; the running image was `29bd8eacf1de`.
- `main` = **`a160e5c6a51065e9e5ee8acfcd1d5a401abba378`** (PR #25). The diff from `79ffa0d` is `scripts/smoke_ws.py` and `tests/unit/test_smoke_ws.py` only (+79/-21): no server code. Checked out detached in `/opt/votebot`; `build` 15 s (cached), exit 0, lowest available memory 2,046 MB (it was 2,167 MB before the build; the abort line is 500 MB). New image **`13e7053fe457`**, tagged `votebot-ddp-next:a160e5c`. `up -d --no-build votebot` only (Redis untouched); healthy after about 40 s; startup line unchanged (`ddp-knowledge-base`, `ocd_bill_id`); `/votebot/v1/health/ready` healthy with pinecone, openai, ddp_openstates_replica and redis.
- Broker, ddp-sync and api-v3 returned 200 before the rebuild.

## The full smoke three times, `--retrieval`
| run | start (UTC) | result | cases that needed another attempt |
|---|---|---|---|
| 1 | 23:57:51 | **6/6**, exit 0 | **FL passed on its 3rd attempt** (attempts 1 and 2: `no answer cited this bill, running the case again`); **WA passed on its 2nd** (attempt 1 uncited) |
| 2 | 23:59:28 | **6/6**, exit 0 | **FL passed on its 2nd attempt** (attempt 1 uncited) |
| 3 | 00:00:42 | **6/6**, exit 0 | none |

- No case failed all of its attempts, and no case failed for any reason other than a missing citation, so there are no failing-case log lines to paste. The retrieval checks, the version-matches-`document_id` check and the live vote tool check passed every time.
- **0 warnings or errors in the container log during all three runs; `Error resolving bill from title` still 0.** Memory after: about 158 MiB of 768, host available about 2,141 MB; health `healthy`.
- I did not measure the OpenAI spend of the runs (the retries add extra chat turns).

## Reading it against your bar
Your bar was every case passing in at least two of three runs without exhausting its attempts. All five cases and the "no votes or diffs" check passed in all three runs, and the most attempts any case used was 3 of 3 (FL in run 1: it passed on the last allowed attempt, so it did not exhaust them, but it is the closest it came). FL needed a retry in two of three runs and WA in one, which is the model leaving `[Source:]` out of a whole case as you explained; I changed no code. **By your rule, gate 3 is closed.**

## What I need now
1. **Gate 5 (nginx, applying broker PR #410 / `de734d7`):** I have not touched nginx. It needs a quiet window the user names, and your runbook template test first (a throwaway nginx container with the same image, mounts and environment; `nginx -t` must say "test is successful"), then recreate only nginx (`--no-deps --force-recreate nginx`: a few seconds of broker downtime), then the baseline checks, `curl https://mapapp.digitaldemocracyproject.org/votebot/v1/features`, and `smoke_ws.py` against `wss://mapapp.digitaldemocracyproject.org/ws/chat`. Is there anything else you want checked before it, for example that the broker checkout on this host already has `de734d7` (I have not pulled it for this)?
2. Still pending from the operator: the logrotate file and `sudo findmnt --verify`.
3. Still open from earlier replies: the render-env PR (`feat/VOTEBOT-14-render-env-from-secrets-manager`, `1fee264`) for your review; a decision on the shared OpenAI, Pinecone and `API_KEY` keys before public launch (you noted it).

Reply on this branch either way.
