# Reply 2 to the prod agent: decisions made, the plan for this host, in order

**Date:** 2026-10-06
**Replies to:** your findings note, and supersedes the "waiting on Ramon" list in reply 1 (its section 3, the read-only collection, still stands and is still needed).
**Ask:** follow the gates below in order. Stop and report at every STOP. Nothing here touches the old civic server.

## Decided by Ramon (2026-10-06)

1. **VoteBot runs as a container, in its own compose project**, on the broker's Docker network. Built **on this host**, like the broker, api-v3 and ddp-sync (no registry, no ECR). It must not be part of the broker's compose project: the broker's deploy recreates containers, which would drop open chats.
2. **nginx stays the front door: the broker's nginx serves VoteBot** as two paths on the broker's existing hostname, `mapapp.digitaldemocracyproject.org` (DNS-only in Cloudflare, so no Cloudflare proxy in the path). **No Cloudflare tunnel, no new hostname, no new certificate, no DNS change.** The nginx change is a PR in the broker repo: `Digital-Democracy-Project/ddp-broker-py` **#410** (adds `location ^~ /ws/chat` and `location ^~ /votebot/`, tested in throwaway containers). Apply it **only after Ramon merges it**, and only at gate 5.
3. **Memory: add a 2 GB swap file now** (gate 1). A resize of the instance happens later in a planned window (it stops the whole box); do not resize.
4. **Namespaces and URLs for the new site (dev for now):** the page lives at `https://dev.digitaldemocracyproject.org`; the chat will use `wss://mapapp.digitaldemocracyproject.org/ws/chat`.

Consequences for the first plan: step 3.5 (systemd) and the Python 3.11 question are gone; step 3.6 (Cloudflare real-IP, a server block) is gone (DNS-only means nginx sees real client addresses directly; VoteBot logs the first `X-Forwarded-For` value); the port question is gone (no host port is needed; nginx reaches the container by name).

## Gate 0: still needed from reply 1 section 3 (read-only)

`uname -m` is no longer needed (the image is built here). **Still needed:** how ddp-sync reaches the broker and api-v3 (URLs, non-secret), the `DisallowedHost` question (see below), the namespace and index ddp-sync writes (`docker exec ddp-sync printenv PINECONE_NAMESPACE KNOWLEDGE_BASE_INDEX_NAME`), the Docker network name(s) and the in-network service names and ports of `web`, api-v3 and nginx, and the check that `ddp-knowledge-base` exists with vectors in that namespace. **STOP if the index or namespace is empty.**

**Broker address warning (unchanged):** the broker's own container health check fails with `DisallowedHost`: Django rejects requests addressed to an internal container name. VoteBot's `DDP_BROKER_API_ROOT` cannot simply be `http://web:8000`. Use the address ddp-sync uses and confirm it serves `GET /api/bills/resolve/`, `/api/organizations/` and `/api/bill-organization-positions/current/`; if nothing works, tell us (the fix is adding the name to the broker's `ALLOWED_HOSTS`, a broker change).

## Gate 1: swap (do first; it also makes the image build safe)

Back up `/etc/fstab` first (`sudo cp -a /etc/fstab /etc/fstab.bak-$(date +%Y%m%d-%H%M)`), then:

```bash
sudo fallocate -l 2G /swapfile        # if fallocate is not supported on this filesystem: sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf && sudo sysctl -p /etc/sysctl.d/99-swappiness.conf
free -m; swapon --show
```

Report `free -m` before and after. Rollback: `sudo swapoff /swapfile && sudo rm /swapfile` and restore the fstab backup. Repeat the baseline health checks of the broker, api-v3 and ddp-sync afterwards.

## Gate 2: build the image on this host

Quiet moment, after gate 1. Use your existing clone at `/opt/votebot`, at an exact commit:

```bash
cd /opt/votebot && git fetch origin && git checkout <the main SHA, currently 72279d8 or later; record it>
```

Create `/opt/votebot-ddp-next/` (owner root) with the compose file below and the `.env` described in the first note's step 3.3 with these differences: `REDIS_URL=redis://votebot-redis:6379/0`, `QUERY_LOG_DIR=/app/logs/queries`, `ALLOWED_ORIGINS=["https://dev.digitaldemocracyproject.org"]`, `DDP_SITE_BASE_URL=https://dev.digitaldemocracyproject.org`, `DDP_BROKER_API_ROOT` and `DDP_OPENSTATES_API_ROOT` as found at gate 0, `PINECONE_INDEX_NAME` and `PINECONE_NAMESPACE` as found at gate 0, Slack tokens present but **empty**, no `WEBFLOW_*` at all, mode 600. **Secrets: ask Ramon how they are provisioned on this host (how does ddp-sync get its keys?); do not write a key value into any note or into a shell history line.** Create the log directory owned by uid 1000: `sudo install -d -o 1000 -g 1000 /opt/votebot-ddp-next/logs/queries`.

```yaml
# /opt/votebot-ddp-next/docker-compose.yml  (host-specific; not in the repo)
name: votebot-ddp-next
services:
  votebot:
    build:
      context: /opt/votebot
      dockerfile: infrastructure/docker/Dockerfile
      target: production
    image: votebot-ddp-next:local
    container_name: votebot-ddp-next          # the broker's nginx (PR #410) addresses it by this name
    env_file: /opt/votebot-ddp-next/.env
    depends_on: [votebot-redis]
    mem_limit: 768m
    restart: unless-stopped
    volumes:
      - /opt/votebot-ddp-next/logs:/app/logs
    networks: [default, broker]
  votebot-redis:
    image: redis:7-alpine
    container_name: votebot-redis
    command: ["redis-server", "--save", "", "--appendonly", "no", "--maxmemory", "128mb", "--maxmemory-policy", "allkeys-lru"]
    mem_limit: 192m
    restart: unless-stopped
networks:
  broker:
    external: true
    name: <the broker's Docker network, from gate 0>
```

Build (`docker compose -f /opt/votebot-ddp-next/docker-compose.yml build`) while watching `free -m`; if available memory falls below about 300 MB, interrupt it (Ctrl-C), report, and wait. Do **not** start the stack yet.

## Gate 3: start it and verify it **inside the Docker network**, with no nginx change

```bash
docker compose -f /opt/votebot-ddp-next/docker-compose.yml up -d
docker logs votebot-ddp-next 2>&1 | grep -E 'VoteBot started|PINECONE_INDEX_NAME|Traceback|Error'
docker exec votebot-ddp-next python -c "import aiofiles, websockets, httpx; print('deps ok')"
docker exec votebot-ddp-next python -c "import httpx; print(httpx.get('http://localhost:8000/votebot/v1/health/ready').text)"
```

The startup line must show `pinecone_index_name=ddp-knowledge-base` and `bill_filter_key=ocd_bill_id`, with no warning about an unrecognised index. Then the smoke test, from inside the container (the image contains `scripts/` and its environment):

```bash
docker exec votebot-ddp-next python scripts/smoke_ws.py --url ws://localhost:8000/ws/chat --cases scripts/smoke_cases.json --retrieval
```

Paste the whole output. Run the two extra checks of the first note's step 4.3 (a legislator and "what changed") the same way with `--min-confidence 0.4` and without `--retrieval`, then the failure paths and a small load check (step 4.4). Watch memory and the baseline services throughout. **STOP on any failure and report it; do not move to nginx.**

## Gate 4: wait for Ramon

Ramon merges broker PR #410 and chooses a quiet window. Do not edit the broker's nginx template yourself.

## Gate 5: nginx (quiet window only)

1. Pull the merged broker change as your normal broker deploy procedure does.
2. **Test the new template before touching the live nginx**, in a throwaway container with the same image, mounts and environment variables as the production nginx service: `docker run --rm <same mounts and -e flags> nginx:stable-alpine nginx -t` must say "test is successful". If not, STOP.
3. Recreate only the nginx container (a few seconds of broker downtime). Immediately repeat the baseline health checks for the broker, api-v3 and ddp-sync, and check the broker's own public routes still answer.
4. Public checks: `curl -s https://mapapp.digitaldemocracyproject.org/votebot/v1/features` (JSON), and `smoke_ws.py` against `wss://mapapp.digitaldemocracyproject.org/ws/chat` (same cases). Rollback if anything else changed status: revert the broker change and recreate nginx; VoteBot keeps running untouched.

## What to report (new file `notes/...-reply-3-...md` on this branch)

The deployed SHA; the network and service names you found; `free -m` before and after swap; the build outcome and peak memory; the startup line; every smoke output (pasted); the baseline-health table before and after each gate; anything that surprised you. No secrets.

## Not for this host / still open

The freeze of the old VoteBot and ddp-sync on the old server (needs someone with access there; Ramon is arranging it). Cutover to the new site, the production origin, and the bot-protection question (this path is not behind Cloudflare's proxy, so Cloudflare bot filtering does not apply here; the plan is to watch the logs first) are Ramon's and are not part of this task.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
