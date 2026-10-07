# Reply 27: please rehearse the VoteBot rollback (approved by the user), after reply 25

Thank you for reply 26: clear and complete. The user approved a **rollback rehearsal of the VoteBot image** (SYNC-92 step 4; there is no soak, so a rollback that has been run once is part of the gate). It restarts only VoteBot, twice, for about a minute each; nobody uses it yet (no website points at it), so no quiet window is needed beyond your usual check. **The nginx rollback is not part of this** and stays unrehearsed until the user names a window.

## Order
1. Do reply 25 first (deploy PR #26, `2ababff`, and its live check). The rehearsal should run on the image that will go live.
2. Then this.

## The rehearsal
Use the image tags you already keep (`votebot-ddp-next:a160e5c` is the previous known-good; the new one is whatever you tag for `2ababff`).
1. **Record before:** running image ID and tag, `docker ps` line, `/votebot/v1/health/ready`, and the baseline 200s (broker `/api/status/`, ddp-sync health, api-v3 `/healthz`).
2. **Roll back:** `docker tag votebot-ddp-next:a160e5c votebot-ddp-next:local`, then `docker compose -f infrastructure/docker/docker-compose.prod.yml up -d --no-build votebot`. Time it from the command to `healthy`. Check that the running image ID is `13e7053fe457` (the old one), the startup line, `/health/ready`, and run the full internal smoke once (`--retrieval`, one attempt is fine). Note: the old image has no fallback citations, so a smoke case that needs citations may need its retries. That is expected, not a failure.
3. **Roll forward:** retag the new image as `:local`, `up -d --no-build votebot`, time it, check the running image ID is the new one, health, and run the smoke once more, plus the public `wss://mapapp.digitaldemocracyproject.org/ws/chat` smoke through nginx (it must work during and after; nginx is not touched, so a chat opened during the restart just fails to reconnect).
4. **Also confirm:** `votebot-redis` was not restarted (its uptime), the broker, ddp-sync and api-v3 baselines are unchanged, and the container log has no new warning or error besides the known `TAVILY_API_KEY` one.
5. **Report:** the exact commands you ran (no values), the time each step took (rollback and roll-forward, command to healthy), the image IDs before, during and after, and anything that differed from the runbook (section 3.4 "Rollback tag"). If a step of the runbook was wrong or missing, say which and I will fix the runbook.

Stop and tell me (leave VoteBot on the new image) if the roll-forward does not come back healthy within about 2 minutes.

## Also, read-only, now
- **Slack key names.** The cutover step needs the two Slack tokens from the shared secret. Please list the JSON KEY NAMES in `ddp-sync/credentials` (names only, no values) so the render script can map them: I assumed `slack_bot_token` and `slack_app_token`. Say if they are called something else, and whether the old VoteBot gets its Slack tokens from that same secret.
- **render-env.** PR #27 on votebot is your `render-env.sh`, rebased and changed: it reads the shared secret by default, fetches inside Python (no secret passes through bash), refuses values with spaces, quotes, `#`, `$` or backslashes (by name, not value), has a `--with-slack` flag for cutover day, and says the file is rewritten in full on every run (so non-secret edits, like the cutover edits to `DDP_SITE_BASE_URL` and `ALLOWED_ORIGINS`, go through `prod.env.defaults` by PR). **Do not re-render `.env` or switch to it until the user merges it.** After the merge: `--check` and `--check --with-slack`, report the names it would write.

## From reply 26: noted, and for the user
The general-page legislator result and the FL positions response are logged as findings. Nothing for you to do.
