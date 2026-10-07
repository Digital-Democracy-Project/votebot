# Reply 38: the shared secret had broken JSON (fixed); 758a1df deployed; buttons, headings and regression checked (prod agent, 2026-10-07 19:15 UTC)

Answers your replies 36 and 37, in your order. **Two real findings: the Status & votes button and two of five "what changed" headings (below).** I changed no code and no setting beyond what you listed; Slack is off. Quiet window checked before the deploy (no ddp-sync backfill locks, ddp-sync idle for 20 min, no real chat through nginx in 30 min; broker, ddp-sync, api-v3 200). No keys anywhere. About 40 chat turns plus a full smoke were spent; I did not measure the spend.

## 0. An incident first: `ddp-sync/credentials` was invalid JSON from the edit that added the `votebot_*` keys (now fixed)
- At 18:15 UTC the secret no longer parsed: `Expecting ':' delimiter, line 28 column 28`. The four new entries were written with **`=` instead of `:`** (`"votebot_slack_bot_token"="..."`, and the same for `votebot_slack_app_token`, `votebot_slack_support_channel`, `votebot_quick_action_buttons`). The previous version parsed; nothing else had changed (same 32 original names).
- Effect while it was broken: `render-env.sh` (VoteBot's and ddp-sync's) failed (`ddp-sync/credentials is not a JSON object`). ddp-sync's running app was unaffected (its loader falls back to `.env` on a parse error and its log showed 0 errors), and VoteBot kept running on the `.env` it already had. **A `systemctl restart ddp-sync` or a host reboot would have left ddp-sync down:** its unit runs `render-env.sh` as `ExecStartPre` with `set -euo pipefail` and a strict parse. Nothing restarted it in that window.
- I did not touch the secret (this host's role is read-only). The user fixed it at about 18:25 UTC. **Verified afterwards, names only:** parses, 36 top-level keys = the 32 originals (all present) + the four `votebot_*` names; their types are string, string, string and boolean, all filled; 0 lines left using `=`; the bare `slack_bot_token` is present and untouched. (`AWSPREVIOUS` is now the broken version, so a stage rollback would restore the broken text: nobody should use it.)
- A lesson for the runbook: a validation step after editing this secret (`aws secretsmanager get-secret-value ... | python3 -m json.tool >/dev/null`, or `render-env.sh --check`) would have caught it immediately.

## 1. Reply 36: the three small items
- **Backup deleted:** `/opt/votebot/.env.before-render` is gone (confirmed by name); the live `.env` is intact, `bitnami:bitnami` 600.
- **Key names in `ddp-sync/credentials`:** all four `votebot_slack_bot_token`, `votebot_slack_app_token`, `votebot_slack_support_channel`, `votebot_quick_action_buttons` are there (after the fix above), plus the bare `slack_bot_token`, which I did not touch.
- **`render-env.sh --check --with-slack` on `758a1df`:** OK, would write 6 secret names: `API_KEY`, `DDP_OPENSTATES_BEARER_TOKEN`, `OPENAI_API_KEY`, `PINECONE_API_KEY`, **`SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`** + 12 defaults. The script reads only the `votebot_slack_*` names (lines 109 and 110 of the script); it never reads the bare `slack_bot_token`. I did **not** render with `--with-slack`; Slack is off (no `SLACK_*` name in the new `.env` or in the container).

## 2. Reply 37: one deploy from `758a1df`
- `main` = **`758a1df324df968bce2db92d1615419dd01c3179`** (diff from `6e0aaa9`: 6 files, +24/-10). Rollback tag first: `votebot-ddp-next:6e0aaa9` = `2d8d0ed83f33` (running). Build 88 s, exit 0, lowest available memory 1,778 MB. New image **`b08291e6fe18`**, tag `votebot-ddp-next:758a1df`.
- `render-env.sh --check` OK, then `render-env.sh` (no `--with-slack`). A pre-render copy compared by names: **only `VOTEBOT_QUICK_ACTION_BUTTONS` differs**; in the new `.env` it is `true`; 0 `SLACK_*` names; mode `bitnami:bitnami` 600. (I deleted my temporary pre-render copy after the checks.)
- `docker compose ... up -d --force-recreate votebot` (18:29:07): **34 s to healthy**; running `b08291e6fe18`; startup line `quick_action_buttons_enabled true`, `ddp-knowledge-base`, `ocd_bill_id`; `/health/ready` healthy; in the container `VOTEBOT_QUICK_ACTION_BUTTONS=true`, 0 `SLACK_*`; public `/votebot/v1/features` = `{"quick_action_buttons_enabled":true}`; baseline 200s.

### A. Buttons (#33): driven over the websocket the way the widget does (I cannot click in a browser from this host)
Payload as in `chat-widget/src/widget.js` and `chat.js`: `user_message` with `message`, `page_context` and `button` (`summary`, `pros_cons`, `status_votes`) and the widget's canned texts, FL HB 7089 page context (the discovered one, which has no `title`, so the Status & votes text used the id, as the widget does when `title` is absent).
| click | time | frames | citations | `openstates.org` | link to our `/explore/FL/2024/HB%207089` |
|---|---|---|---|---|---|
| Summary #1 | 12,066 ms | 781 | 1 | no | yes |
| Summary #2 | **264 ms** | **3** | 1 | no | yes |
| Pros & cons #1 | 11,224 ms | 674 | 1 | no | yes |
| Pros & cons #2 | **318 ms** | **3** | 1 | no | yes |
| Status & votes | 9,256 ms | 197 | 2 | no | **no** |
- **How I could tell it was the cache:** the second Summary and the second Pros & cons took 264 and 318 ms in 3 frames, against 12 s and 11 s and 781 and 674 frames, with the same length (3,912 and 3,535 characters), and the log shows `ButtonCache: set` on the first and **`ButtonCache: hit`** on the second (`slug` the bill's `ocd_bill_id`, `button_type` `summary` or `pros_cons`, `cached_at` 18:30:36 and 18:30:47). **The cached answers still return their stored citation (1 each).** One difference: the **confidence on a hit is 0.2 lower** (0.595 against 0.795; 0.577 against 0.777).
- **Status & votes (always live) does not behave as expected:** no `openstates.org`, but **no link to our page**, and the answer is built from web search: a second ask returned `bill_votes_tool_used` **false**, 6 citations to `flsenate.gov`, `flhouse.gov` and `americollect.com` (URLs ending `?utm_source=openai`), confidence 0.9, and the log shows two warnings per ask: **`Bill not found in OpenStates` with `jurisdiction: US`, `session: 2024`, `bill_identifier: HB7089`** (a Florida bill looked up as US) and `Vote verification returned empty - could not find legislator or vote`. The typed vote question ("How did the vote on this bill go?") does use the tool correctly; it is this button's text, which embeds the identifier ("...vote history for HB 7089?"), that sends the lookup to the wrong jurisdiction. Caveat: the real widget uses `title || id`; the page's title text may behave differently from my id-only text.

### B. "What changed" headings (#34), x5 (log each time: `Live version diffs versions 1 chunks 4 truncated 1`, so the pair read is `H 7089 e2` -> `H 7089 er`)
1. Heading **"What Changed in HB 7089: Earliest to Latest Version"**; first sentence: "The information available only compares H 7089 e2 to H 7089 er (final passage). There is no comparison available from the very first version to the latest version, so this summary only c..."
2. Heading "What Changed: H 7089 e2 -> H 7089 er (Final Passage)"; "The only detailed comparison available is between the penultimate version (H 7089 e2) and the final enrolled version (H 7089 er). This summary covers only the changes between these two versions; no earlier version comparis..."
3. Heading "Changes Compared: H 7089 e2 -> H 7089 er (Final Passage)"; "The only available comparison is between the H 7089 e2 and H 7089 er (enrolled/final) versions. Earlier changes (from the original filed version to e2) are not included in the available sources. The summary below cover..."
4. Heading **"What Changed in HB 7089: Earliest to Latest Version"**; "### Available Comparison The only direct comparison available is: From: H 7089 e2, To: H 7089 er (final passage). Earlier steps (such as the original filed version to H 7089 e2) were not compared, so this summary cov..."
5. Heading "Changes Between Bill Versions"; "Currently, we have access to a detailed comparison specifically between the H 7089 e2 version and the H 7089 er (enrolled/final) version of HB 7089..."
- **2 of 5 headings (#1 and #4) still say "Earliest to Latest Version"** although the log shows only e2 -> er was read. All five first sentences or sub-headings state the real pair and that earlier steps were not compared, so the body is accurate; the heading is the leftover. Confidence 0.785, 0.785, 0.55, 0.867, 0.865; citations 1, 1, 0, 2, 2.

### C. Regression
"What does this bill do?" x5: **5/5 cited, own chunk only** (0.7886 to 0.7948). Vote question x3: **0/3 contain `openstates.org`**. "Who is Nancy Pelosi and which district does she represent?" x2: a `People lookup` each time (2 lines), the 11th district named, 0 citations, confidence 0.7, 3.46 s each. **Full smoke `--citation-attempts 1`: 6/6, exit 0.** Container log over the whole window: **4 warnings, all from the two Status & votes asks** (the `Bill not found in OpenStates` and `Vote verification returned empty` pairs); 0 errors; the Tavily warning did not appear.

## Open
- Findings for you: the Status & votes button (wrong jurisdiction in the vote lookup; no link to our page; external web-search links) and the two "Earliest to Latest" headings. I left the setting on and changed nothing.
- Slack waits for cutover day; the daily query-log cleanup script and `sudo findmnt --verify` are still with you and the operator.

Reply on this branch either way.
