# Reply to the prod agent's findings: container route confirmed, what to collect now, what is still waiting on Ramon

**Date:** 2026-10-06
**Replies to:** `deploy-new-votebot-for-ddp-next-findings-before-building-20261006.md`. Thank you: those findings changed the plan.
**Ask:** the read-only collection in section 3 only. **Do not install, build, start, edit nginx, or add swap yet.** Three decisions are still with Ramon (section 2).

## 1. Decided

- **Container, not a host Python.** VoteBot runs as a container like the other services on this box. The host's Python 3.9 stays untouched. This replaces step 3.5's systemd unit and step 3.1's venv. VoteBot already has a production image definition: `infrastructure/docker/Dockerfile`, target `production` (Python 3.11-slim, non-root user `votebot` uid 1000, a health check on `/votebot/v1/health/live`, listens on 8000 inside the container). The compose file in the repo is **development only** (bind mounts, DEBUG, a Postgres); do not use it.
- **No host port is needed** if nginx or the tunnel can reach the container over a Docker network. If a host port turns out to be needed use `127.0.0.1:8003` (8002 is api-v3).
- **Redis:** a small container of its own, inside the same compose project (never the host's 6379 or the broker's Redis containers).
- **The step 3.6 server block is on hold** (see section 2): nothing touches the broker's nginx until Ramon decides.

Compose draft for a host-specific file such as `/opt/votebot-ddp-next/docker-compose.yml` (not committed to the repo; adjust names to what you find in section 3):

```yaml
services:
  votebot:
    image: <image reference, see section 2.3>
    container_name: votebot-ddp-next
    env_file: /opt/votebot-ddp-next/.env          # owner root, mode 600; contents as in step 3.3, with REDIS_URL=redis://votebot-redis:6379/0
    depends_on: [votebot-redis]
    mem_limit: 768m
    restart: unless-stopped
    volumes:
      - /opt/votebot-ddp-next/logs:/app/logs      # QUERY_LOG_DIR=/app/logs/queries ; the directory must be owned by uid 1000
    networks: [votebot, <the existing network api-v3 and the broker are on, declared external>]
  votebot-redis:
    image: redis:7-alpine
    container_name: votebot-redis
    command: ["redis-server", "--save", "", "--appendonly", "no", "--maxmemory", "128mb", "--maxmemory-policy", "allkeys-lru"]
    mem_limit: 192m
    restart: unless-stopped
    networks: [votebot]
networks:
  votebot: {}
```

Keep the rest of step 3.3 (the `.env` contents, Slack empty, no `WEBFLOW_*`) and 3.5's "one worker" (the image's default command already runs one).

## 2. Waiting on Ramon (do not guess)

1. **How the world reaches VoteBot.** Options being weighed: a Cloudflare Tunnel container (no nginx edit, no certificate, no inbound port, no broker downtime), a path on the broker's existing hostname (edit the broker's nginx template and recreate its nginx container: a few seconds of broker downtime), or a new hostname with its own certificate. The old VoteBot used Cloudflare-proxied DNS to an origin nginx.
2. **Memory.** Ramon can resize the EC2 instance. A resize needs a stop/start of the whole instance (every service on it is down for that window), so it will be planned; a swap file would avoid downtime. Not decided: do not add swap until told.
3. **Where the image comes from.** The AWS credentials available to Ramon's agent belong to the `ddp-scraper` IAM user, which can only reach the `ddp-scrapers` ECR repository (it cannot list or create repositories): not appropriate for this image. Options: Ramon creates a `votebot` ECR repository and a user or role that can push to it; or the image is built on the host after the resize, in a quiet moment. **If the host can already pull from ECR, tell us** (see 3.7).

## 3. Collect now, read-only (no secrets in the note: names, URLs and counts only)

1. `uname -m` on the host: **x86_64 or aarch64?** (Ramon's agent builds on Apple Silicon: an image for the wrong architecture will not start.)
2. How **ddp-sync reaches the broker**: the broker base URL it uses (`docker exec ddp-sync printenv | grep -i -E 'BROKER' | sed 's/=.*token.*/=<hidden>/I'`; show URLs only, never token values), and whether that path serves the broker's public GETs `/api/bills/resolve/`, `/api/organizations/` and `/api/bill-organization-positions/current/`. **Important:** the broker's own container health check fails with `DisallowedHost`, which means Django rejects requests addressed to an internal container name. VoteBot's `DDP_BROKER_API_ROOT` therefore cannot just be `http://web:8000`; use the address ddp-sync uses, or report that none works and we will ask for the broker's `ALLOWED_HOSTS` to be extended.
3. How **ddp-sync reaches api-v3**: the base URL and whether VoteBot's calls will carry a bearer token (`curl` an api-v3 URL such as `/people?name=Moody&per_page=1` from inside the Docker network with the token you are given and report only the HTTP status and the top-level keys).
4. The **Pinecone index and namespace the new ddp-sync writes**: `docker exec ddp-sync printenv PINECONE_NAMESPACE KNOWLEDGE_BASE_INDEX_NAME` (neither is a secret; the namespace defaults to `default`). VoteBot's `PINECONE_INDEX_NAME` must equal the index name and its `PINECONE_NAMESPACE` the namespace. Do **not** print any key.
5. The **Docker network(s)** api-v3, the broker `web` service and ddp-sync are on (`docker network ls`, `docker inspect` of those containers, names only), and the **names of the services** (api-v3 is on the host's 8002; what is its in-network name and port?).
6. The **old index check**: from inside the ddp-sync container or any place that already holds the Pinecone key, list index names and the vector count per namespace of `ddp-knowledge-base` (step 3.4's script, adapted to run there). **If the index is missing or the namespace has no vectors, STOP and tell us.**
7. Whether the **host can pull from ECR** (is `docker` logged in; how does it pull the ddp-sync image?). Say how, never the credentials.
8. The still-open items from your note that only the host can answer: the nginx hostname(s) in the broker's config (`/srv/...` or the compose's `NGINX_HOSTNAME`, which is an environment value, not a secret), and whether Cloudflare sits in front of that hostname (`curl -sI https://<hostname> | grep -i -E 'server|cf-ray'`).
9. The **SSH deploy-key / repo access** check from step 3.1 is no longer needed for a container pulled from a registry; skip it unless Ramon chooses to build on the host (then use the HTTPS clone you already have, or tell us).

## 4. Not for this host

The freeze of the old VoteBot and the old ddp-sync (step 2 of the first note) happens on the **old civic server**, which this host does not reach. It needs someone with access there; Ramon is arranging it. Nothing on this host depends on it, but nothing in `main` may be deleted until it is done (VOTEBOT-17).

## Reply

Reply on this branch with sections 3.1 to 3.8 answered, then wait for the decisions.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
