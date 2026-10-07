# Reply 23: gate 5 done: the broker's nginx now serves VoteBot; the public smoke passed 6/6 (prod agent, 2026-10-07 00:06 UTC)

Follows reply 22. Done on the operator's instruction ("proceed to gate 5 if we're in a quiet window"). Times UTC.

## Pre-flight (00:03 to 00:04)
- Quiet window: no ddp-sync backfill locks, ddp-sync idle for 45 min (only health checks in its log), broker checkout clean at `1fcff46d`. One celery tally task was active; an nginx-only recreate does not touch celery. ECS tasks could not be checked (this host's role is denied `ecs:ListClusters`).
- What the pull brought (`git diff HEAD origin/main`): exactly 3 files, +324/-2: `infra/nginx/templates/default.conf.template` (+66, the VoteBot routes), `ddpbroker/search_eval/judged_queries.json` and one test (OPEN-326 eval data). No migrations, requirements, Dockerfile or compose changes.
- **Template test before touching the live nginx** (your runbook step): a throwaway `nginx:stable-alpine` container on `ddp-broker-py_default` with the live nginx's mounts (certs, `.well-known`, static roots, the templates directory taken from git at the commit being tested, so the live checkout was not touched), the same six `-e` variables, and `nginx -t`. **Control** (the current template, `1fcff46d`): `syntax is ok`, `test is successful`. **New template (`de734d70`)**: `syntax is ok`, `test is successful`. Note: the live nginx bind-mounts the templates directory but only renders it at container start, so pulling changed nothing running.

## Deploy
- `git pull --ff-only` in `/opt/ddp-broker-py`: `1fcff46d` -> **`de734d70921f4de9b6f7a9c422b7c0a5d952dfe6`** (PR #410 plus #409).
- `docker compose -p ddp-broker-py --env-file /opt/ddp-broker-py/.env -f infra/compose/prod.yml up -d --no-deps --force-recreate nginx` at 00:04:04; nginx up at 00:04:13 (about 9 s of broker nginx downtime). Restart count 0; `nginx -t` inside the new container: successful; 8 `votebot` lines in the rendered config (it had 0 before); 0 `[emerg]/[alert]/[crit]/[error]` lines since.
- Only nginx was recreated: web, celery, celery-beat, redis, ddp-sync, api-v3 and VoteBot were not restarted.

## Baseline health, before and after the recreate (identical)
Broker `/api/status/` 200 | `/api/organizations/` 200 | `/api/search/?q=hb1` 503 (expected, search off) | ddp-sync health 200 | api-v3 `/healthz` 200 | VoteBot healthy. The broker `web` container still shows `unhealthy` (the known `DisallowedHost` healthcheck), unchanged. Final check at 00:05:57: the same, and host available memory about 2,185 MB.

## Public checks (through nginx; no website points at it yet)
- `GET https://mapapp.digitaldemocracyproject.org/votebot/v1/features` -> **200** `{"quick_action_buttons_enabled":false}` (the same through `https://localhost` with the Host header). `/votebot/v1/health/live` 200; `/health/ready` 200 `healthy` with all dependencies.
- **CORS:** `Origin: https://dev.digitaldemocracyproject.org` -> exactly **1** `access-control-allow-origin` header (that origin); `Origin: https://evil.example.com` -> **0** headers.
- **WebSocket smoke over the public URL** (`scripts/smoke_ws.py --url wss://mapapp.digitaldemocracyproject.org/ws/chat --cases scripts/smoke_cases.json --retrieval`, run from inside the VoteBot container): **6/6 cases passed, exit 0**; WA passed on its 2nd attempt, the others on the first. nginx logged **18 `GET /ws/chat` upgrades with status 101**, and 0 nginx errors; VoteBot logged 0 warnings or errors during the run. I did not measure the OpenAI spend.
- The broker's own routes still answer. `GET /` returns 404 (it proxies to Django, which has no root route); I did not baseline `/` before the change, but the `location /` blocks in the old and new templates are the same.

## State now
VoteBot `votebot-ddp-next` runs image `13e7053fe457` (= tag `a160e5c`; earlier tags `79ffa0d`, `493f362`, `ba86a14` kept for rollback), healthy, reachable at `wss://mapapp.digitaldemocracyproject.org/ws/chat` and `https://mapapp.digitaldemocracyproject.org/votebot/`. `/opt/votebot/.env` holds ddp-sync's credentials by the operator's decision (one OpenAI key shared by three services, a Pinecone key that can write, and ddp-sync's own `api_key` as VoteBot's). Not done and not mine: the website's `wsUrl` (NEXT-36), the cutover, and the production origin.

## Rollback (not needed; not rehearsed)
Nginx: `cd /opt/ddp-broker-py && git checkout 1fcff46d`, then the same `up -d --no-deps --force-recreate nginx` command; VoteBot keeps running either way. VoteBot: `docker tag votebot-ddp-next:<previous sha> votebot-ddp-next:local` then `up -d --no-build votebot`, or `docker compose ... down`.

## Open
- **Still pending from the operator:** the logrotate file for `/opt/votebot-logs/queries/*.jsonl` and `sudo findmnt --verify` (the swap line). Query logs have been writing since the first chat; there is no rotation yet.
- For you: review of the render-env PR branch (`feat/VOTEBOT-14-render-env-from-secrets-manager`, `1fee264`), and the decision on dedicated OpenAI, Pinecone and `API_KEY` keys before public launch.
- The broker checkout is now on `de734d70` (it was on the OPEN-326 deploy `1fcff46d` before), so the next broker deploy starts from there.

Reply on this branch either way.
