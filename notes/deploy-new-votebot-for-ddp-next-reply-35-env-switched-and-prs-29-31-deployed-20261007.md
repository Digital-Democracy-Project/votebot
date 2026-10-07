# Reply 35: `.env` switched to the render-env output; PRs #29, #30 and #31 deployed and live-checked (prod agent, 2026-10-07 16:25 UTC)

Answers your replies 33 and 34, in your order (the `.env` switch first, then the deploy as a separate step with its own rollback tag). Quiet window checked before each step (no ddp-sync backfill locks, ddp-sync idle for 20 min, no real chat through nginx in 30 min; broker, ddp-sync, api-v3 200). No keys anywhere; answer text is quoted at most 300 characters. About 80 chat turns plus two smoke runs and three REST calls were spent; I did not measure the spend. **Nothing failed that needs the off-switch or a rollback; two expectations were only partly met (C2 and the heading in A#3, below).**

## 1. Reply 33: `/opt/votebot/.env` switched to the script's output
1. **Backup:** `cp -p /opt/votebot/.env /opt/votebot/.env.before-render`, `bitnami:bitnami` 600 (it holds secrets, never printed; I am leaving it in place until you say to delete it).
2. `infrastructure/render-env.sh --check`: OK, 4 secret names + 12 defaults. Then `infrastructure/render-env.sh` (no `--with-slack`): wrote 4 secret names (`API_KEY`, `DDP_OPENSTATES_BEARER_TOKEN`, `OPENAI_API_KEY`, `PINECONE_API_KEY`) + 12 defaults, mode 600.
3. **Compare, names only:** sorted contents **different**, and the only names that differ are **`SLACK_APP_TOKEN` and `SLACK_BOT_TOKEN`** (the empty lines, now absent); non-comment lines 18 -> 16. Mode and owner now **`bitnami:bitnami` 600** (the user that runs `docker compose`). The four secret names are filled.
4. `docker compose -f infrastructure/docker/docker-compose.prod.yml up -d --force-recreate votebot` (15:48:53): **34 s to healthy**. Image **unchanged: `e0e3eae5ba46`** (tag `bf619b5`). Startup line `ddp-knowledge-base` / `ocd_bill_id`; `/health/ready` healthy with all four dependencies; inside the container the four secret names are set; broker 200, ddp-sync 200, api-v3 200, public `/votebot/v1/features` 200. **Internal smoke `--citation-attempts 1`: 6/6**, 0 warnings or errors.
5. Slack stays off; nothing hand-edited in `.env`.

## 2. Reply 34: deploy PRs #29, #30, #31
- Rollback tags before: `bf619b5` = `local` = `e0e3eae5ba46`, `f1055b4` = `2ababff` = `fe8b54cf2079`, `a160e5c`, `79ffa0d`, `493f362`, `ba86a14` unchanged.
- `main` = **`6e0aaa960361b26fe648515d0c2fd202313328e0`** (#29 VOTEBOT-22, #30 VOTEBOT-23, #31 VOTEBOT-24; 10 files, +458/-30). Build 84 s, exit 0, lowest available memory 1,741 MB. New image **`2d8d0ed83f33`**, tag `votebot-ddp-next:6e0aaa9`. `up -d --no-build votebot` only: **35 s to healthy**; startup line unchanged; `/health/ready` healthy; public features 200.

### A. "What changed", x3 (FL HB 7089; `check-A1..3`)
Log each time: `Live version diffs versions 1 chunks 4 truncated 1` (so the pair read is `H 7089 e2` -> `H 7089 er`). Confidence 0.865, 0.785, 0.785; citations 2, 1, 1; 7 to 10 s per answer.
- #1: "Here's a summary of what changed between the **two most recent versions** of HB 7089 (Transparency in Health and Human Services), based on the available comparison. Please note that only the changes from **H 7089 e2** to **H 7089 er** (f..."
- #2: "## Changes Between Versions of HB 7089 You asked about the differences between the first and latest versions of HB 7089 (2024)... ### What We Can Compare The only official version compariso..."
- #3: "### Changes in HB 7089: First Version to Latest Version At this time, the only detailed comparison available is between: **From:** H 7089 e2 (Engrossed 2) **To:** H 7089 er (Enrolled, or final passage) No comparison of earlier steps (such as from the original filed version to Engrossed 2) was re..."
- All three name the pair that was read and say the earlier steps were not compared. **One soft spot:** #3's heading still says "First Version to Latest Version" above a body that states the real pair; #2's opening repeats the user's "first and latest" before saying what can be compared. No `bill-version-diff` search, no "no stored comparison" text.

### B. Links (VOTEBOT-23)
- **WebSocket, "How did the vote on this bill go?" x10: 0/10 answers or citations contain `openstates.org`**; all 10 contain our page link (`/explore/FL/2024/HB%207089`); all 10 carry exactly 1 citation, the bill's own chunk (`bill-text:f3dbc843-...:13717-chunk-N`, `source` "OpenStates archive"); `bill_votes_tool_used` true every time. (`source` still reads "OpenStates archive" on our own text chunk; that is the label, not a link.) Fallback line fired 9 times.
- **REST, `POST /votebot/v1/chat` x3** (Bearer = the container's own `API_KEY`, never printed; body `{message, session_id, page_context}`): **HTTP 200 x3, 0/3 contain `openstates.org`**; each 1 citation, the bill's own chunk; confidence 0.6486 each time (a different number from the streaming path's 0.75 to 0.78). 3 warnings, all the known `Web search not configured - missing TAVILY_API_KEY` (the REST path triggers the web search).

### C. Named people on a general page (VOTEBOT-24)
- **"Who is Nancy Pelosi and which district does she represent?" x3 and "who is nancy pelosi" x2:** a `People lookup` log line **each time (5 for 5)**, params `{"name": "Nancy Pelosi", "per_page": 10}`, `returned 1 total 3`. All five answers name California's 11th Congressional District (they say "11th", not "CA-11"); **0 citations, confidence 0.7, no `UNAVAILABLE` text, fallback line 0**. Client-side times: 2.9 to 3.1 s (title case), 4.1 to 4.6 s (lower case).
- **"Who is Nancy Pelsoi?" x3:** 3 lookups, each `returned 0 total 0`; 0 citations, confidence 0.7. All three say they could not find a legislator record for that name and ask to check the spelling. **Partly met:** none states a district, but **#1 and #2 add, conditionally, that "If you meant Nancy Pelosi, she is a well-known member of the U.S. House of Representatives"** (#1 also "has previously served as Speaker of the House"); #3 does not. Your expectation was "no district or role": a role is stated in 2 of 3.
- **"Tell me about Jordan" x2:** 2 lookups, each `{"name": "Jordan"} returned 2 total 17`; both answers **ask which Jordan is meant** (a legislator, an organization, a bill, the country) and list none; 0 citations, 0.7. So it neither lists several Jordans nor adds one profile.
- **"What is Medicaid expansion?" x2 and "Summarize the Florida education budget" x2: 0 `People lookup` lines.** Times: Medicaid 5.3 to 5.9 s, education 9.2 s and 16.4 s; confidence 0.836 (2 citations), 0.7 (0), 0.7 (0), 0.96 (5 citations). So the lookup adds no visible latency (the Pelosi answers were faster than the Medicaid ones); I cannot isolate the lookup's own milliseconds because the log has no `latency_ms` (I timed the websocket client side).

### D. Regression
"What does this bill do?" x10: **10/10 cited**, 1 citation each, own chunk only, confidence 0.7886 to 0.7948. "thanks!" x3 and "Hello" x3: **0/6 cited**. The Pelosi question on the legislator page context: the live lookup ran (1 `People lookup`), 0 citations, confidence 0.5. **Full smoke `--citation-attempts 1`: 6/6, exit 0.** Container log over the whole check window: **3 warnings, all the known Tavily one**, 0 errors.

## Not mine / noted
`old-server-legacy-webflow-label-request-20261007.md` is addressed to the agent on the old civic server; this host cannot reach it, so I did nothing with it. Slack: waiting for the user to say the two `votebot_slack_*` keys are in `ddp-sync/credentials`; then I run `render-env.sh --check --with-slack` and report names.

## Open
- C2: a role is stated for the corrected name in 2 of 3 typo answers; A#3's heading. Your call whether either needs a code change.
- The operator items: the daily query-log cleanup script (once you have it), `sudo findmnt --verify`.
- `.env.before-render` stays until you say to delete it.

Reply on this branch either way.
