# Runbook: running VoteBot for the new website (ddp-next) on the new-infrastructure server

Ticket: VOTEBOT-14. Blocks NEXT-36 (chat on the new site) and SYNC-92 (the cutover).
Run it through the prod agent (`ddp-broker-py` `notes/ops-handoff`). **Nothing in this document has been run.**
It was written from the repos only: the live nginx configs and real `.env` files were not available, so every
step starts with a read-only check and every value that is not in a repo is marked `<...>`, not guessed.

## 0. The layout (decided by Ramon, 2026-10-05)

| | Old VoteBot (Webflow) | New VoteBot (ddp-next) |
|---|---|---|
| Server | the existing civic server | the new-infrastructure server: ddp-broker-py, ddp-open-states, ddp-sync (new), and this |
| Code | frozen on the branch `legacy-webflow`, nothing merged into it | `main` (Webflow code is still in it but never runs here; removing it is VOTEBOT-17) |
| How it runs | systemd on the old server | **a container**, in its own compose project, built on the host |
| Front door | the old server's nginx; `votebot.digitaldemocracyproject.org`, proxied by Cloudflare | **the broker's nginx**: two paths on `mapapp.digitaldemocracyproject.org` (DNS-only in Cloudflare) |
| Index | `votebot-large` | `ddp-knowledge-base` |
| Data pipeline | the old ddp-sync, also frozen on a branch | the new ddp-sync on the new server |
| Slack handoff | stays here | **off** until the new copy gets its own Slack setup (only one copy may hold the Slack connection) |
| Webflow | still used | **none at all** |

Decisions recorded:

- **Second copy of VoteBot, built for the new infrastructure only: yes.** There is no second copy on the old
  server and no `/next` path. (The earlier plan for that is gone.)
- **Rate limiting of connections: no, not now.** Monitor first (section 5). Bot protection for suspicious
  traffic, without affecting real visitors, is section 5 (Cloudflare first, an invisible check later if needed).
- **Container, own compose project, built on this host** like the broker, api-v3 and ddp-sync (the host's Python
  is 3.9; VoteBot needs 3.11 or newer). Not part of the broker's compose project: the broker's deploy recreates
  containers, which would drop open chats.
- **The broker's nginx serves VoteBot** as `/ws/chat` and `/votebot/` on the broker's existing hostname, through a
  change in the broker repo (`ddp-broker-py` PR #410). No tunnel, no new hostname, no new certificate. That hostname
  is DNS-only, so Cloudflare's proxy (and its bot filtering) is not in the path.
- **Memory: swap file now (2 GB), instance resize later in a planned window** (a resize stops the whole box).
- **Answers link to our own pages**: `DDP_SITE_BASE_URL` (VOTEBOT-15 part 2).
- **Organization positions from our own data**: ddp-broker-py (VOTEBOT-15 part 2).

What the new VoteBot does to the broker, so the shared host is not at risk from it: only public, unauthenticated
**GET** reads (`/api/bills/resolve/`, `/api/bills/{id}/scorecard/`, `/api/bill-organization-positions/current/`,
`/api/organizations/{id}/` and `/positions/`). It holds no broker credential, writes nothing, and starts no job.

Order of work: section 1 (read-only), section 2 (freeze the old copy), section 3 (install the new one),
section 4 (the website and its origins), then 5 (bot protection and monitoring), 6 (verify), 7 (cutover and rollback).

**Ground rules for every change on a host.** Back up first, with a timestamp, and note the backup in the ticket
(`sudo cp -a <file> <file>.bak-$(date +%Y%m%d-%H%M)`). The broker's nginx runs in a container whose template is read
at start, so a change means testing the template in a throwaway container first and then recreating that container
(see 3.7); afterwards re-check that the routes that worked before still do. Abort and
roll back (restore the backups, reload/restart, repeat the earlier checks) if a route that worked changes
status, a restarted service does not come back within a minute, free memory drops as in 1.1, disk use grows
unexpectedly, or an answer comes from the wrong index. The prod agent may abort at any point and should report
what was and was not changed.

**Baseline health of everything else on the server.** Before the first change, write down that these answer
(whatever the real checks are for the broker, api-v3/ddp-open-states and ddp-sync: a status endpoint or a `curl`
and the container health of each, `docker ps`) and repeat them after installing Redis, after starting VoteBot, after every
nginx reload and after cutover. A VoteBot-only test cannot show damage to its neighbours.

| Service | Check | Baseline | After Redis | After VoteBot | After nginx | After cutover |
|---|---|---|---|---|---|---|
| broker |  |  |  |  |  |  |
| ddp-open-states / api-v3 |  |  |  |  |  |  |
| ddp-sync |  |  |  |  |  |  |

## 1. Read-only checks on the new server

### 1.1 Room, ports and what is already there

```bash
free -m; swapon --show; df -h /; nproc
ss -ltnp | grep -E ':(80|443|8000|8001|8002|6379)\b'     # what already listens where
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'      # the services here run as containers, not under systemd
systemctl list-units --type=service --state=running | grep -E 'redis|nginx' || true
ps -eo pid,rss,cmd --sort=-rss | head -12                  # the biggest memory users right now
```

Expected from what Ramon described: the box is mostly ddp-sync starting Fargate jobs, so it is largely idle; the
one heavy job is scorecard creation, which is moving to Fargate. VoteBot is small (it mostly waits on OpenAI,
Pinecone and the broker). If free memory is thin **while a scorecard run is going**, either wait for that job to
move or add memory; do not guess.

| Check | Found |
|---|---|
| free memory and swap, idle and during a scorecard run |  |
| the broker's Docker network name, and the in-network names and ports of `web`, api-v3 and nginx (api-v3 listens on the host's 8002; no host port is needed for VoteBot) |  |
| is there a Redis, on which port, and what uses it |  |
| nginx: which hostnames/server blocks exist, who owns the config |  |
| is Cloudflare in front of this server's hostnames |  |

### 1.2 What VoteBot will call

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<broker port>/api/organizations/   # the broker, locally: 200
curl -s "http://127.0.0.1:<broker port>/api/bills/resolve/?jurisdiction=FL&session=2026&gov_id=HB%201" # 200 {"bill_openstates_id": "<uuid>"} or 404 {"detail": "Bill not found"} (ddp-broker-py `resolve_bill`: GET, public, all three parameters required)
```

Record the broker's local URL (this becomes `DDP_BROKER_API_ROOT`; calling it over the local network instead of
the public internet is faster and removes a public dependency) and the ddp-open-states api-v3 URL and bearer token
(`DDP_OPENSTATES_API_ROOT`, `DDP_OPENSTATES_BEARER_TOKEN`; version-aware answers need api-v3's version fields).

| Value | Found |
|---|---|
| `DDP_BROKER_API_ROOT` (local URL) |  |
| `DDP_OPENSTATES_API_ROOT`; token set? |  |
| the new site's hostnames: dev (`dev.digitaldemocracyproject.org`) and production |  |
| where ddp-next serves static files (for the widget bundle, 4.2) |  |

### 1.3 The old server, for the freeze

```bash
cd ~/votebot && git status -sb && git log -1 --format='%h %cd %s'
systemctl cat votebot.service | head -30
cd <ddp-sync checkout> && git status -sb && git log -1 --format='%h %cd %s'
```

| Check | Found |
|---|---|
| votebot checkout path, current commit, clean? |  |
| ddp-sync checkout path, current commit, clean? |  |

## 2. Freeze the old copy (old server)

The old VoteBot and the old ddp-sync keep running exactly as they are. They stop receiving updates, so they must
stop being deployed from `main`.

1. On GitHub, from the commit the old server runs today, create a branch and a tag in **both** repos:
   `legacy-webflow` (branch) and `legacy-webflow-2026-10` (tag). A branch can move, so **record the full
   commit SHA** of each in the ticket as the identity of what is running. Nothing is merged into the branch
   afterwards except an emergency fix agreed with Ramon.
2. On the old server, check each checkout out on `legacy-webflow` and leave it there. Record the checkout paths
   and that "deploy to the old server = `git pull` on `legacy-webflow` only" in the ticket (never `main`, which
   no longer has Webflow). Also save, in the ticket, copies of each service's unit file (`systemctl cat`), the
   path and permissions of its `.env` (not the contents), the Python version and `pip freeze`, and the exact
   restart commands, so the old copy can be rebuilt if the server is.
3. The chat widget file is shared by both sites. Anything merged to `main` that changes it must keep working with
   the old server's page-context shape (it does today: Webflow fields are passed through unchanged).
4. Slack human handoff stays with the old copy. Do not set Slack tokens on the new copy (see 3.3).

Rollback of the whole project is "send the website back to the old copy": it is untouched and still running.

## 3. Install the new VoteBot (new server)

Everything on this host runs in Docker and is built on the host, and the host's Python (3.9) is too old for VoteBot
(3.11 or newer), so VoteBot is a container too: **its own compose project** in `/opt/votebot-ddp-next/`, joined to the
broker's Docker network, reached by the broker's nginx by its container name `votebot-ddp-next`. VoteBot is the only
part of this server that takes anonymous input from the public; the container runs as a non-root user (uid 1000) and
publishes no host port.

### 3.1 Memory first: a 2 GB swap file (decided 2026-10-06)

About 2.4 GB is available and there is no swap. Back up `/etc/fstab`, then:

```bash
sudo fallocate -l 2G /swapfile        # if unsupported on this filesystem: sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf && sudo sysctl -p /etc/sysctl.d/99-swappiness.conf
free -m; swapon --show
```

Rollback: `sudo swapoff /swapfile && sudo rm /swapfile` and restore the fstab backup. A later instance resize is a
planned, whole-box downtime and is not part of this runbook.

### 3.2 Build the image on the host

```bash
cd /opt/votebot && git fetch origin && git checkout <the main SHA to deploy; record it>
docker compose -f /opt/votebot-ddp-next/docker-compose.yml build      # after 3.3 and 3.4; watch `free -m`
```

The repo's own `infrastructure/docker/Dockerfile`, target `production` (Python 3.11-slim, non-root, a health check on
`/votebot/v1/health/live`). Do **not** use the repo's `docker-compose.yml`: it is development only. Build in a quiet
moment after 3.1; if available memory falls below about 300 MB, stop the build and report.

### 3.3 `/opt/votebot-ddp-next/.env` (owner root, mode 600; a fresh file, nothing copied from the old server)

```bash
ENVIRONMENT=production
API_KEY=<new random key>
OPENAI_API_KEY=<a key from a dedicated OpenAI project for this deployment>
PINECONE_API_KEY=<...>
PINECONE_INDEX_NAME=ddp-knowledge-base          # exactly this; the index name ddp-sync writes (KNOWLEDGE_BASE_INDEX_NAME)
PINECONE_NAMESPACE=<ddp-sync's PINECONE_NAMESPACE; default is "default">
DDP_BROKER_API_ROOT=<an address that serves the broker's public GETs and is not rejected as DisallowedHost; see 1.2>
USE_DDP_OPENSTATES_REPLICA=true
DDP_OPENSTATES_API_ROOT=<api-v3's in-network address>
DDP_OPENSTATES_BEARER_TOKEN=<...>
DDP_SITE_BASE_URL=https://dev.digitaldemocracyproject.org     # production host once decided; unset is a safe no-op
ALLOWED_ORIGINS=["https://dev.digitaldemocracyproject.org"]   # JSON array; REPLACES the code default; no Webflow origins
REDIS_URL=redis://votebot-redis:6379/0
QUERY_LOG_DIR=/app/logs/queries
VOTEBOT_QUICK_ACTION_BUTTONS=<same value as on the old server>
SLACK_BOT_TOKEN=
SLACK_APP_TOKEN=
```

No `WEBFLOW_*` variables at all. The empty Slack lines are deliberate (one Slack connection per app token; human
handoff stays with the old copy). **How secrets reach this file follows however ddp-sync's are provisioned on this
host; never write a key value into a note, a ticket or shell history.** The broker address warning: the broker's own
health check fails with `DisallowedHost` because Django rejects requests addressed to an internal container name, so
`DDP_BROKER_API_ROOT` cannot simply be `http://web:8000`; use the address ddp-sync uses, or have the broker's
`ALLOWED_HOSTS` extended.

### 3.4 The compose project

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
    container_name: votebot-ddp-next          # the broker's nginx addresses it by this name
    env_file: /opt/votebot-ddp-next/.env
    depends_on: [votebot-redis]
    mem_limit: 768m
    restart: unless-stopped
    volumes:
      - /opt/votebot-ddp-next/logs:/app/logs
    logging: {driver: json-file, options: {max-size: "10m", max-file: "3"}}
    networks: [default, broker]
  votebot-redis:
    image: redis:7-alpine
    container_name: votebot-redis
    command: ["redis-server", "--save", "", "--appendonly", "no", "--maxmemory", "128mb", "--maxmemory-policy", "allkeys-lru"]
    mem_limit: 192m
    restart: unless-stopped
    logging: {driver: json-file, options: {max-size: "10m", max-file: "3"}}
networks:
  broker:
    external: true
    name: <the broker's Docker network>
```

VoteBot has a Redis of its own (never the broker's, never the host's 6379): only a button cache and a handoff map
live in it, so no persistence. **One worker, on purpose**: a session's chat history is kept in the memory of the
worker that served it, and the image's default command runs one. Restarting the container ends open chats and their
history: expected, and the widget reconnects.

### 3.5 Start it and verify it INSIDE the Docker network, before any nginx change

```bash
sudo install -d -o 1000 -g 1000 /opt/votebot-ddp-next/logs/queries
docker compose -f /opt/votebot-ddp-next/docker-compose.yml up -d
docker logs votebot-ddp-next 2>&1 | grep -E 'VoteBot started|PINECONE_INDEX_NAME|Traceback|Error'
docker exec votebot-ddp-next python -c "import aiofiles, websockets, httpx; print('deps ok')"
docker exec votebot-ddp-next python -c "import httpx; print(httpx.get('http://localhost:8000/votebot/v1/health/ready').text)"
```

The startup line must show `pinecone_index_name=ddp-knowledge-base` and `bill_filter_key=ocd_bill_id`, with no warning
about an unrecognised index (a wrong name makes Pinecone create an EMPTY index: confirm the index exists with vectors
in the namespace first, read-only, as in section 1.2's check). Then section 6, run from inside the container.

### 3.6 Logs

Query logs are one JSONL file per day in `/opt/votebot-ddp-next/logs/queries` (visitors' messages and addresses). Keep
14 days and compress, so they neither fill the shared disk nor keep messages longer than needed:

```conf
# /etc/logrotate.d/votebot-ddp-next
/opt/votebot-ddp-next/logs/queries/*.jsonl {
    daily
    rotate 14
    compress
    missingok
    notifempty
}
```

Container logs are rotated by the `json-file` options in 3.4. Check `df -h /` after the first week and name who looks.

### 3.7 nginx: the broker's, through the broker repo (PR #410)

The broker's nginx container renders its template when it starts. VoteBot's two locations (`/ws/chat`, `/votebot/`) are
in `ddp-broker-py` PR #410, written with the broker's own patterns: the upstream address is a variable (BROKER-115), so
nginx starts without VoteBot and finds a recreated container again; and the locations declare their own `add_header` so
the wide-open CORS header set at the top of that server is not added a second time to VoteBot's responses (two
`Access-Control-Allow-Origin` headers make browsers reject them). Apply it **only after VoteBot is verified in 3.5 and
the PR is merged**, in a quiet window:

1. Test the merged template in a throwaway container with the production nginx service's image, mounts and environment:
   `docker run --rm <same mounts and -e flags> nginx:stable-alpine nginx -t` must report "test is successful".
2. Recreate only the nginx container (a few seconds of broker downtime), then repeat the baseline health checks and check
   the broker's own public routes.
3. Public checks: `curl -s https://mapapp.digitaldemocracyproject.org/votebot/v1/features` and the smoke test against
   `wss://mapapp.digitaldemocracyproject.org/ws/chat`.

Rollback: revert the broker change and recreate nginx; VoteBot keeps running untouched. No DNS or certificate work is
needed: `mapapp.digitaldemocracyproject.org` and its certificate already exist.

## 4. The website

### 4.1 Allowed origins

Only the HTTP calls (`/content/resolve`, `/features`) are subject to the browser's origin check; the socket is
not, and VoteBot has no origin check on it. `ALLOWED_ORIGINS` on the new copy lists only new-site origins
(`https://dev.digitaldemocracyproject.org` now). **At cutover add the new site's production origin: exact scheme
and host, to be supplied by NEXT-36 / Ramon.** Webflow origins are not listed here; they belong to the old copy.

### 4.2 The chat widget bundle

The widget file is the same for both copies. Find where ddp-next serves static files (1.2) and where the bundle
should live there; record the answer and the copy procedure in `chat-widget/README.md` ("How a change reaches
production"). If the file is served through Cloudflare's proxy, purge its cache after every copy, and in any case
check the served file's hash equals the repo's (`sha256sum chat-widget/dist/ddp-chat.min.js` against `curl -s <url> | sha256sum`).

The new site configures the widget with the new copy:

```javascript
window.DDPChatConfig = { wsUrl: 'wss://mapapp.digitaldemocracyproject.org/ws/chat', pageContext: { type: 'bill', id: 'HB 219',
  jurisdiction: 'FL', session: '2026', ocd_bill_id: '<uuid>' } };
```

(or only a `?ddp_url=` link, which `/content/resolve` turns into the same thing). The address lives in the new
site's own page configuration, not in the bundle (the bundle's built-in default is the old production address
and only applies when a page sets none). So moving users, and moving them back, is a change to ddp-next's
configuration and its deploy. Find out how long that takes and write it down: a tab that already has the chat
open keeps its old connection until it is reloaded.

## 5. Bot protection and monitoring (no limits now)

There is no limit on opening chat connections, by decision. Every chat message costs OpenAI and Pinecone calls, so
watch for abuse and have a plan. **The VoteBot path is on `mapapp.digitaldemocracyproject.org`, which is DNS-only in
Cloudflare: Cloudflare's proxy, and so its built-in bot filtering, is not in the path.** nginx sees real visitor
addresses directly (VoteBot logs the first `X-Forwarded-For` value).

**Watch (a few minutes a week at first).** Messages per visitor address, from VoteBot's own logs (check the field name
with `head -1` first):

```bash
cd /opt/votebot-ddp-next/logs/queries
jq -r 'select(.event_type=="message_received") | .client_ip' $(date +%F).jsonl | sort | uniq -c | sort -rn | head
```

Look for one address sending hundreds of messages, many short sessions from one address, or identical messages. Also
set a **monthly usage limit and an email alert in the OpenAI dashboard**: it is the one real backstop for cost, needs no
code, and works whatever the cause. Give the new VoteBot **its own OpenAI project and API key** so the limit applies to
it alone, and find out whether reaching the limit only alerts or actually blocks requests.

**If bots show up, in this order:**

1. **Cloudflare Turnstile (a small code change).** An invisible check that runs in the browser when the chat opens: free,
   and only suspicious visitors ever see a prompt. The widget obtains a token and VoteBot verifies it with Cloudflare
   before accepting the connection. It does **not** need the hostname to be proxied. Open a ticket then.
2. **A proxied hostname for the chat** (orange cloud), which brings Cloudflare's bot filtering and rate rules: needs a
   name for it, a certificate for that name on the broker's nginx, and a Cloudflare setting that lets the socket
   through. Also possible when ddp-next serves the site from this server and the site's own hostname is proxied.
3. A per-visitor cap on messages (application code) only if an abuser opens one connection and floods it.

## 6. Verify

**What this section needs from code.** `scripts/smoke_ws.py` (VOTEBOT-16, in `main`). Links to our own `/explore/...` pages need `DDP_SITE_BASE_URL` and organization positions from the broker need `DDP_BROKER_API_ROOT`; both are VOTEBOT-15 part 2 (PR #16, merged). Legislator answers and "what changed" read api-v3 live (VOTEBOT-15 part 3, PR #17) and need `USE_DDP_OPENSTATES_REPLICA=true`. Deploy a version of `main` that contains all of them before running this section, or the checks below will fail for the right reason. The startup line to check (`docker logs votebot-ddp-next | grep 'VoteBot started'`) is a structured log that includes `pinecone_index_name=ddp-knowledge-base` and `bill_filter_key=ocd_bill_id` (`src/votebot/main.py`).

```bash
# first INSIDE the Docker network (before the nginx change), then through nginx
docker exec votebot-ddp-next python scripts/smoke_ws.py --url ws://localhost:8000/ws/chat --cases scripts/smoke_cases.json --retrieval
docker exec votebot-ddp-next python scripts/smoke_ws.py --url wss://mapapp.digitaldemocracyproject.org/ws/chat --cases scripts/smoke_cases.json
```

(the image contains `scripts/` and the container has its `.env`, which `--retrieval` needs; the README's "WebSocket Smoke Test" explains
the checks; the canonical index must already contain embedded bills, since Pinecone creates a missing index on
first use and an empty one answers nothing). It prints the index and namespace it read; confirm they are
`ddp-knowledge-base` and the namespace the new ddp-sync writes, and that an answer never cites old-index content.

Failure paths, once, before cutover: stop the broker briefly in a quiet moment (or point `DDP_BROKER_API_ROOT` at a
closed port and restart) and confirm answers still come, just without organization positions; restart VoteBot in
the middle of a chat and confirm the widget reconnects; send an `/content/resolve` request from an origin that is
not allowed and confirm it is refused; confirm `docker logs votebot-ddp-next` shows no Slack or Webflow activity. A small load check:
run the smoke script from a few terminals at once and watch memory, CPU, `df` and the broker's response time; abort
if the broker slows noticeably. Then, in a browser on the dev origin: open a bill page with the chat,
ask a question, and confirm the answer links to our own `/explore/...` page, the "who supports" question lists
organizations from the broker, and the console shows no CORS error. In `docker logs votebot-ddp-next`, check there is **no**
"Slack service started" line and that the request was logged by this copy.

## 7. Cutover and rollback

- **Cutover** (SYNC-92 owns the criteria and the soak): point the new site's `wsUrl` (and any `?ddp_url=` links)
  at `wss://mapapp.digitaldemocracyproject.org/ws/chat`, add the production origin (4.1), purge the widget cache if it
  is proxied. The Webflow site keeps using the old copy meanwhile (its page contexts need the old index). Once Webflow
  is retired, repointing `votebot.digitaldemocracyproject.org` (proxied, currently at the old server) at this server is
  an alternative cutover lever, but it needs that name and its certificate on the broker's nginx.
- **Rehearse first, on the dev site**: switch it to the new copy and back, timing each way, including how long
  ddp-next takes to deploy the change and a tab reload, and test the old copy right before (its chat, its index,
  Slack handoff, the Webflow page). Do not cut over until the way back has been shown to work.
- **Go/no-go**: go only if section 6 passed, the baseline health table is unchanged after every step, and the
  rehearsal worked. Roll back at once if after cutover chat errors repeat, the broker slows, memory or disk
  pressure appears, an answer comes from the wrong index, or Slack/Webflow activity shows up on the new copy.
- **Rollback**: point the new site's `wsUrl` back to the old copy and purge the cache. Nothing on the old server
  was changed, so there is nothing to restore. To remove the new copy entirely: `docker compose -f
  /opt/votebot-ddp-next/docker-compose.yml down`, revert the broker nginx change and recreate nginx (3.7).

## 8. Checklist for the ticket

- [ ] 1.1 to 1.3 filled in, differences reported
- [ ] Section 2: `legacy-webflow` branch and tag in votebot and ddp-sync, old checkouts pinned, paths recorded
- [ ] 3.1 swap added; 3.2 to 3.5: image built on the host, container running and verified INSIDE the Docker network (`bill_filter_key=ocd_bill_id` in the startup log, no Slack), own Redis container, memory limits and log rotation in place
- [ ] 3.7: broker PR #410 merged, template tested in a throwaway container, nginx recreated in a quiet window, public smoke test passing
- [ ] Baseline health table filled in and unchanged after each step
- [ ] 4.1 production origin supplied and added at cutover; 4.2 widget path documented in `chat-widget/README.md`
- [ ] Section 5: weekly monitoring owner named; OpenAI usage limit and alert set; Turnstile or a proxied hostname only if needed
- [ ] Section 6 verified with real bills, failure paths and the small load check done; rollback rehearsed on the dev site
- [ ] Hand back to NEXT-36 (which `wsUrl`) and SYNC-92
