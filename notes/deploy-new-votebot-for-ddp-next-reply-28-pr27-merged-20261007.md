# Reply 28: PR #27 (render-env) is merged. Small update to replies 25 and 27.

votebot `main` is now **f1055b4** (PR #26 citations + PR #27 render-env/runbook; the only change since `2ababff` is `infrastructure/render-env.sh`, `infrastructure/docker/prod.env.defaults`, its tests and the runbook, no server code). Build reply 25's deploy from **f1055b4** instead of `2ababff`, so the checkout has the script; record the SHA and tag the image as before. Replies 25 and 27 are otherwise unchanged and their order stands (deploy and live check, then the rollback rehearsal).

## After that: validate the script without touching the live `.env` (read-only for the running service)
1. `infrastructure/render-env.sh --check` and `infrastructure/render-env.sh --check --with-slack`: report the key NAMES each would write, and, if `--with-slack` fails, which Slack key names are missing (that tells us the real names in the secret).
2. Render to a temporary file and compare it with the live one **without printing any value**: `infrastructure/render-env.sh --out /opt/votebot/.env.check`, then compare `sort /opt/votebot/.env | sha256sum` with `sort /opt/votebot/.env.check | sha256sum` and report only equal or different (if different, report only the key NAMES that differ, e.g. by comparing `cut -d= -f1`). Then delete `/opt/votebot/.env.check`. The expected difference, if any, is ordering and the removed empty `SLACK_*=` lines; the secret values should match.
3. Do not render over `/opt/votebot/.env` and do not restart anything for this. Switching to the script's output is a separate step the user will name.

Everything else from replies 25 and 27 stands: Slack key names (names only), the rollback rehearsal, gate 5 done, and the operator items (logrotate, `findmnt --verify`, the `legacy-webflow` freeze) still open.
