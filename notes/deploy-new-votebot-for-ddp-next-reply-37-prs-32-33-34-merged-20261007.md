# Reply 37: PRs #32, #33 and #34 are merged. Deploy, then run the checks below (2026-10-07)

votebot `main` is now **758a1df324df968bce2db92d1615419dd01c3179** (#32 Slack key names, #33 buttons on, #34 "what changed" headings). This replaces the "separate deploys" order in reply 36: all three landed together, so **one build from this SHA**. The previous running image (`2d8d0ed83f33`, tag `6e0aaa9`) is the rollback.

## Order
1. **Names first (read-only; if you have not done them from reply 36):** delete `/opt/votebot/.env.before-render`; list the key **names** in `ddp-sync/credentials` and say whether `votebot_slack_bot_token`, `votebot_slack_app_token`, `votebot_slack_support_channel`, `votebot_quick_action_buttons` are there. Never print a value.
2. **Slack check:** `git pull --ff-only`, then `infrastructure/render-env.sh --check --with-slack`. Report names only. It must list `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` and must not have read the bare `slack_bot_token`. Do **not** render with `--with-slack`, and Slack stays off.
3. **Deploy:** tag the running image first, build from `758a1df`, `render-env.sh --check`, then `render-env.sh` (no `--with-slack`), then `up -d --force-recreate votebot`. Record the image ID and time to healthy. In the new `.env`, confirm by name that `VOTEBOT_QUICK_ACTION_BUTTONS=true` and that no `SLACK_*` name is present.

## Checks (FL HB 7089 page context; session ids `check-N`; names, counts, and at most 300 characters of an answer)
**A. Buttons (#33):** `/votebot/v1/features` returns `quick_action_buttons_enabled: true`. On the dev site, on a bill page, **Summary** x2 and **Pros & cons** x2 (the second of each from the cache; say how you could tell, for example the log or the response time), **Status & votes** x1 (always live). No `openstates.org` in any answer; each links to our own `/explore/...` page. If you cannot click in the browser, drive the same button requests over the websocket the way the widget does, and say which you did.

**B. Headings (#34):** "What changed between the first and the latest version of this bill?" **x5**. Quote each **heading and first sentence**. Expect none to say "first to latest" or "original to final" unless the log line `Live version diffs versions ...` shows that was the pair read.

**C. Regression:** "What does this bill do?" x5 (cited, own chunk only); the vote question x3 (0 `openstates.org`); "Who is Nancy Pelosi?" x2 (lookup line, CA-11); the full smoke with `--citation-attempts 1` (expect 6/6). Container log: 0 warnings or errors beyond the known Tavily one.

## Rules
If any check fails, do not change code or settings: report the evidence. If the container is not healthy within about 2 minutes, roll back to the `6e0aaa9` tag (runbook) and tell me. Rollback for the buttons alone: set the line back to `false` in `prod.env.defaults` by a PR and re-render.

Still with me: the daily query-log cleanup script and `sudo findmnt --verify`. Reply on this branch.
