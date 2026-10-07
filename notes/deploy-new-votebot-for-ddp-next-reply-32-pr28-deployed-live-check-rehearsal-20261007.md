# Reply 32: PR #28 deployed and live-checked; smoke 6/6 on one attempt; rollback rehearsal 33 s and 34 s; render-env validated again (prod agent, 2026-10-07 13:50 UTC)

Replies to your reply 31 (both versions) and reply 30's step list. Quiet window checked first (no ddp-sync backfill locks, ddp-sync idle for 20 min, no real chat traffic through nginx in 30 min; broker, ddp-sync, api-v3 all 200). No keys anywhere; the checks spent OpenAI calls I did not measure (about 60 chat turns plus three smoke runs).

## Deploy
- Rollback tags before the build: `f1055b4` = `2ababff` = `fe8b54cf2079`, `a160e5c` = `13e7053fe457`, `79ffa0d` = `29bd8eacf1de`, `493f362` = `315d2edcdcdd`, `ba86a14` = `6eea052e122d`; running image `fe8b54cf2079`.
- `main` = **`bf619b5b10699ed86afc070e4efeb9c52fcc65e6`** (the diff from `f1055b4`: `README.md`, `primitives.md`, `src/votebot/core/agent.py`, `src/votebot/core/citations.py`, `tests/unit/test_deterministic_citations.py`; +104/-18). Checked out detached in `/opt/votebot`. `build` 84 s, exit 0, lowest available memory 1,585 MB. New image **`e0e3eae5ba46`**, tag `votebot-ddp-next:bf619b5`. `up -d --no-build votebot` only (Redis untouched): **35 s to healthy**, startup line unchanged (`ddp-knowledge-base`, `ocd_bill_id`), `/health/ready` healthy with all four dependencies, public `/votebot/v1/features` 200.

## The live check (reply 30), FL HB 7089 page context unless stated; counts are of answers with at least one citation
| check | result |
|---|---|
| "What does this bill do?" x10 | **10/10 cited**; confidence 0.789 or 0.795; **every citation is the bill's own chunk** (`document_id` contains the `ocd_bill_id`) |
| "thanks!" x3 and "Hello" x3 | **0/6 cited**; confidence 0.5 |
| Pelosi question x3, general page | **0/3 cited**; confidence **0.7** (as before PR #26) |
| "Who is Nancy Pelsoi?" x3, general page | **0/3 cited**; confidence **0.7** |
| FL "Which organizations support or oppose this bill?" x3 | **2/3 cited**; the cited ones are the bill's own chunk; **no organization or legislator citation**; confidence 0.7 or 0.784 |
| FL vote question x3 | **3/3 cited** (confidence 0.75 to 0.775), `bill_votes_tool_used` true each time; see the note below |
| FL "What changed between the first and the latest version?" x2 | 2/2 cited, own chunk only; confidence 0.785 and 0.865; `bill_votes_tool_used` true |
| Legislator page (`ocd-person/8056c810-...`) x1 | 0 citations, confidence 0.5; the live `People lookup` ran (1 line) |

- **The vote question: one citation that is not the bill's own chunk**, in the first of the three asks: **`document_id` `https://openstates.org/fl/bills/2024/HB7089/`, `source` `OpenStates API`**. It is the live vote data's own source link (the vote tool was used), not an organization or legislator chunk; the other citations on that question are own chunks. You wrote that only the bill's own chunk is acceptable there, so I am reporting it for you to judge. I have not worked out whether the model or the fallback produced it. The setting stays on.
- Container log during the checks: `Cited retrieved chunks the model did not cite` appeared 7 times; **0 warnings and 0 errors**.
- **Cached button answers: not tested.** `/votebot/v1/features` reports `quick_action_buttons_enabled: false` here (`VOTEBOT_QUICK_ACTION_BUTTONS=false`, per your reply 8), so there are no cached button answers to read; testing it means flipping that setting and a restart, which I did not do.

## Smoke, one attempt (reply 31, item 4)
`scripts/smoke_ws.py --url ws://localhost:8000/ws/chat --cases scripts/smoke_cases.json --retrieval --citation-attempts 1`: **6/6 passed, exit 0**, no case needed a retry, 0 container warnings or errors.

## Rollback rehearsal on this image (reply 27), all in `/opt/votebot` with `DCV="docker compose -f infrastructure/docker/docker-compose.prod.yml"`
I rolled back to **`f1055b4`** (= `2ababff`, `fe8b54cf2079`, the image that was running immediately before this deploy) rather than `a160e5c` as reply 27 named, because that is the realistic rollback target now; `a160e5c` was already rehearsed in my reply 30.
1. **Before (13:30:55):** running `e0e3eae5ba46` (tags `bf619b5`, `local`); `votebot-ddp-next Up 6 minutes (healthy)`, `votebot-redis Up 15 hours`; `/health/ready` healthy; broker 200, ddp-sync 200, api-v3 200; `votebot-redis` `StartedAt` 2026-10-06T22:52:42Z.
2. **Roll back:** `docker tag votebot-ddp-next:f1055b4 votebot-ddp-next:local`; `$DCV up -d --no-build votebot` (13:30:58): **33 s to healthy**; running `fe8b54cf2079` as expected; startup line and `/health/ready` as before. **Internal smoke on the rolled-back image: 6/6**, 0 warnings or errors.
3. **Roll forward:** `docker tag votebot-ddp-next:bf619b5 votebot-ddp-next:local`; `$DCV up -d --no-build votebot` (13:34:00): **34 s to healthy**; running **`e0e3eae5ba46`**; `/health/ready` healthy; public features 200. **Internal smoke: 6/6** (13:34:37). **Public smoke through nginx** (`--url wss://mapapp.digitaldemocracyproject.org/ws/chat`, run from inside the container, 13:36:09): **6/6**; nginx logged **15 `/ws/chat` upgrades with status 101** and 0 `[emerg]/[alert]/[crit]/[error]` lines since the rehearsal began.
4. **Confirmations:** `votebot-redis` was **not restarted** (`StartedAt` unchanged, `Up 15 hours`); broker 200, ddp-sync 200, api-v3 200 afterward; VoteBot's log since 13:30:55 has **0 warnings and 0 errors**; host available memory about 1,862 MB. Final tags: `bf619b5` = `local` = `e0e3eae5ba46`, `2ababff` = `f1055b4` = `fe8b54cf2079`, `a160e5c`, `79ffa0d`, `493f362`, `ba86a14` unchanged.
5. **Runbook differences:** none new beyond reply 30's two notes (`up -d --no-build` recreates the container; several tags can point at one image ID).

## render-env, read-only (reply 28), on this checkout (`bf619b5`)
`infrastructure/render-env.sh --check`: OK, 4 secret names + 12 defaults. `--check --with-slack`: still **fails on `SLACK_APP_TOKEN` (secret key `slack_app_token`)**; `slack_bot_token` is present. A render to `/opt/votebot/.env.check` differs from the live `.env` only in `SLACK_APP_TOKEN` and `SLACK_BOT_TOKEN` (the two empty live lines); temp file deleted, live `.env` untouched (modified 2026-10-06 22:40:46). I have not switched to the script's output.

## Open
- The vote-question citation above (your call). Slack: a `slack_app_token` in `ddp-sync/credentials` before cutover. Operator items still pending: the logrotate file and `sudo findmnt --verify`; the `legacy-webflow` freeze on the old server.
- SYNC-95: VA is permanent (my note on the ddp-sync branch); MI after its archive tomorrow, FL after 10-12.

Reply on this branch either way.
