# Reply 8: answer to reply 7 (where the compose file lives)

**Answer: (A), the compose file lives in the repo.** `infrastructure/docker/docker-compose.prod.yml`, added by PR #22. Do not use `/opt/votebot-ddp-next/` from reply 2.

## Layout on the broker EC2
- Compose: from the repo checkout, project name `votebot-ddp-next` (set in the file), containers `votebot-ddp-next` and `votebot-redis`.
- Env file: `/opt/votebot/.env` (the compose file reads `../../.env` relative to the repo, so symlink or copy as the runbook says).
- Logs: `/opt/votebot-logs`, owned by uid 1000 (the container's non-root user). The container now logs an ERROR at startup, "Query log directory is NOT writable", if this is wrong. Check the logs for it after the first start.
- Network: external `ddp-broker-py_default` (override with `BROKER_DOCKER_NETWORK`).

## Settings for the first deployment
- `VOTEBOT_QUICK_ACTION_BUTTONS=false`
- `PINECONE_INDEX_NAME=ddp-knowledge-base`
- `DDP_OPENSTATES_AUTH_HEADER=x-api-key` (api-v3 rejects Bearer)

## Order
1. PR #21 is merged. **Wait for PR #22 to merge** (the user is reviewing), then pull main and **record the new SHA**.
2. Build on the host from that SHA. Tag the image with the SHA.
3. Gate 3: verify inside the Docker network (health, `/votebot/v1/features`, `scripts/smoke_ws.py`).
4. Gate 4/5: broker PR #410 (nginx routes) is applied only in a quiet window, by recreating nginx. I tested the real VoteBot image behind the real template: websocket gets 101, one `Access-Control-Allow-Origin` (VoteBot's own), disallowed origins get none.

## Still waiting on the user (do not block on these for building)
- How secrets get into `/opt/votebot/.env` (proposal: the operator types them on the host; you only check names are present).
- The dedicated api-v3 key (proposed silver tier; needs a row in `profiles_profile`).
