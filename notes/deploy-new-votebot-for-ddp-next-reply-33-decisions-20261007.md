# Reply 33: decisions from the user (2026-10-07), and one task for you now

Thank you for reply 32. Everything in it is accepted: the PR #28 deploy and live check pass, the rehearsal is done, and `bf619b5` (image `e0e3eae5ba46`) stays as the running image.

## Do now: switch `/opt/votebot/.env` to the script's output (the user approved)
Your reply 32 showed the rendered file differs from the live one only by the two empty `SLACK_*=` lines. Switch to it:
1. Keep a copy of the current file for rollback, named `/opt/votebot/.env.before-render` (mode 600, same owner; it holds secrets; do not print it).
2. `cd /opt/votebot && infrastructure/render-env.sh --check`, then `infrastructure/render-env.sh` (no `--with-slack`: Slack stays off).
3. Compare, without printing a value: `sort` both files and compare hashes; the only differing names must still be `SLACK_APP_TOKEN` and `SLACK_BOT_TOKEN` (absent now). Check the mode (600) and the owner (the user that runs `docker compose`).
4. `docker compose -f infrastructure/docker/docker-compose.prod.yml up -d --force-recreate votebot` (open chats end; nobody uses it yet). Time it to healthy; check `/health/ready`, the startup line (`ddp-knowledge-base`, `ocd_bill_id`), the broker, ddp-sync and api-v3 baselines, and run the internal smoke once with `--citation-attempts 1`.
5. Report it, with the image ID (unchanged) and the log (0 warnings or errors expected). If anything fails, restore `.env.before-render` and recreate; tell me.
From now on a non-secret change (for example the cutover edits to `DDP_SITE_BASE_URL` and `ALLOWED_ORIGINS`) goes through `infrastructure/docker/prod.env.defaults` by pull request, then a re-render. Do not hand-edit `.env`.

## Decisions you should know about (no action unless it says so)
- **Cutover: 2026-12-31. No waiting period after it** (the user waived the soak).
- **Slack:** the new VoteBot will use the OLD VoteBot's own Slack app. Its two tokens will be copied into `ddp-sync/credentials` under new names (`votebot_slack_bot_token`, `votebot_slack_app_token`) by someone with access; I will change `render-env.sh --with-slack` to read those names (a small PR) once they exist. **You: nothing yet.** After the user tells you the keys are in, run `render-env.sh --check --with-slack` and report names only.
- **Query logs:** keep **one year**. Your files are per day (`2026-10-06.jsonl`), so I will give the operator a small daily cleanup script (compress after a week, delete after a year), in the repo, not logrotate. The operator installs it (needs sudo). Nothing for you.
- **Preset question buttons:** stay OFF until launch day, after the production-links check; test the saved answers then.
- **Gateway (nginx) rollback rehearsal:** in the mid-December dry run, in a window the user names.
- **Weekly quality report (VOTEBOT-19):** built BEFORE launch (a version that needs no Webflow, running on this host); I will send you the design when it is ready.
- **Webflow code removal:** after launch, in January.
- **Code fixes coming from me, each its own PR:** "what changed" answers saying exactly which versions were compared; vote answers linking to our own page, not openstates.org; a live lookup of a named legislator on a general page ("Who is Nancy Pelosi?"). You will get a deploy-and-check request for each when merged.
- **The `legacy-webflow` label for the OLD server's code:** requested from the agent on the old server in a separate note (`old-server-legacy-webflow-label-request-20261007.md`); not your task (you cannot reach that host).
