# Reply 36: decisions on reply 35. Delete the backup, Slack key names, buttons on for the dev site, "what changed" headings (2026-10-07)

Thank you for reply 35. Nothing needed a rollback. My decisions, in the order you listed the open items. Three of them wait for a merge; I will say when each is merged. **Do not merge anything yourself.**

## 1. Typo names ("Nancy Pelsoi"): no change
A guessed "if you meant Nancy Pelosi, she is..." line is acceptable. No code change. Nothing for you to do.

## 2. Delete `/opt/votebot/.env.before-render` now
The new file has been through two restarts and a 6/6 smoke test, and the only difference was the two empty Slack lines. Delete the backup and confirm by name only that it is gone.

## 3. Slack keys: use the `votebot_` names, leave `slack_bot_token` alone
I added `votebot_slack_bot_token` and `votebot_slack_app_token` (and `votebot_slack_support_channel`, `votebot_quick_action_buttons`) to AWS Secrets Manager. **Which secret I added them to is something I have not confirmed**: your first step is to list the key **names** (never values) in `ddp-sync/credentials` and report whether the four `votebot_*` names are there.

The secret already holds a bare `slack_bot_token` that belongs to something else. Do not touch it, and make sure VoteBot never reads it. PR #32 changes `render-env.sh --with-slack` to read the `votebot_slack_*` names (the old names were never to be used). After I tell you #32 is merged: pull, run `render-env.sh --check --with-slack`, report the names only, and **do not turn Slack on**. Slack still waits for cutover day and for the old copy's connection to be confirmed stopped.

`votebot_slack_support_channel` and `votebot_quick_action_buttons` are **not read by the script, on purpose** (neither is a secret). Ignore them for now.

## 4. Quick-action buttons: on for the dev site, then test
PR #33 sets `VOTEBOT_QUICK_ACTION_BUTTONS=true` in `prod.env.defaults` (the setting lives there, not in the secret). After I tell you #33 is merged: pull, `render-env.sh --check`, render, `up -d --force-recreate votebot` (tag the running image first, as before). Then:
- `/votebot/v1/features` returns `quick_action_buttons_enabled: true`.
- On the dev site, on a bill page: click **Summary** twice and **Pros & cons** twice (the second of each should come from the cache; say how you could tell), and **Status & votes** once (always live). No `openstates.org` link in any answer; each answer links to our own `/explore/...` page.
- Report names, counts and the first 300 characters of an answer at most. If the buttons do not show or an answer is wrong, do not change code or the setting; report it. Rollback: set the line back to `false` in the defaults file by a PR and re-render.

## 5. "What changed" headings (PR #34)
PR #34 adds a rule that a heading or opening line may name only the two versions actually compared. After I tell you #34 is merged and you have deployed it (separate deploy, own rollback tag): ask "What changed between the first and the latest version of this bill?" **5 times** on FL HB 7089 and quote **each heading and first sentence**. Expect none to say "first to latest" unless the log shows that was the pair read.

## Order and rules
Do 2 and the name listing in 3 now. Then 3 (check), 4, 5, each only after I say its PR is merged, each as its own deploy. If any check fails, do not change code or settings; report the evidence and leave things as they are unless the container is not healthy within about 2 minutes (then roll back to the previous tag and tell me).

## Still open, with me
The daily query-log cleanup script and `sudo findmnt --verify`.

Reply on this branch.
