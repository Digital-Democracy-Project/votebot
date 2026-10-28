# Runbook: running VoteBot for the new website (ddp-next) on the new-infrastructure server

Ticket: VOTEBOT-14. Blocks NEXT-36 (chat on the new site) and SYNC-92 (the cutover).
Run it through the prod agent (`ddp-broker-py` `notes/ops-handoff`).

**Status 2026-10-12** (taken from the prod agent's and the old-server agent's replies on `notes/ops-handoff`; not re-run when
this was written). **Run on the broker host:** 3.1 (swap), 3.2 to 3.5 (image built on the host, compose project, `.env`
rendered with `render-env.sh`, container verified inside the Docker network), 3.7 (the broker's nginx routes), 3.8
(`WEB_CONCURRENCY=1`, 1 GiB memory limit), the section 6 verification (smoke test 6 of 6 on every deploy) and a rollback
rehearsal (about 33 s each way). **Deployed now:** `main` `4472180` (VOTEBOT-42, the same-session reconnect fix, on top of
the VOTEBOT-34 multi-worker work) in the held container: healthy, one worker, no site points at it. Its checks on 2026-10-12:
smoke test 6 of 6 inside the network and 5 of 5 over public wss, and the same-session reconnect repro answered 12 of 12 at
gaps of 0, 50, 200 and 1000 ms. **Old server:** section 2 is partly done: the `legacy-webflow` **branch** exists and the old
checkout runs it (`7864abc`), but there is **no tag** and the ddp-sync checkout was not covered (see section 2).
**Not run:** the log-cleanup install in 3.6 (needs sudo, VOTEBOT-20), section 4 (the website and the widget: NEXT-36), section
7 (the cutover: SYNC-92). Section 5's weekly check and the OpenAI usage limit are not recorded in the notes this status was
written from.

The runbook was first written from the repos only (the live nginx configs and real `.env` files were not available), so
every step starts with a read-only check and every value that is not in a repo is marked `<...>`, not guessed.

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

What the broker has to serve for the answers (public GETs, nothing else): the list above, plus `/api/bill-artifacts/current/` and
`/api/concept-statements/` (LegBot's analysis and the concept poll of a bill) and the two **scorecard routes by OpenStates UUID**,
`/api/bills/{uuid}/scorecard/` and `/api/legislators/{uuid}/scorecard/` (BROKER-325, VOTEBOT-60). The scorecard routes are on the
dev broker; **as of 2026-10-27 they were not yet deployed to the production broker** (from the BROKER-325 hand-off, not checked from
this repo). A broker without them is not an outage: the lookup returns nothing and VoteBot answers as it did before (1.2 checks a
route, 6 the answer).

**Status after 2026-10-12.** The status paragraph at the top is as of 2026-10-12. `main` has since gained VOTEBOT-44 to 53 (timing
fields, concurrent lookups, background warm-up, cheaper `/health/ready`), 58 and 59 (OpenStates only from the DDP replica) and 60 (scorecards).
**Deployed 2026-10-28 01:21 UTC: `740fc1d8b40a66b627b2af2e1907cb6afaf2f434`** (image `2878c1dff616`, tags `:740fc1d` and `:local`; healthy 32 s
after `up -d`, `WEB_CONCURRENCY=1`). Rollback images on the host: `:4472180` (`2a0f548f3ab2`) and `:d3742ae` (`4b6ffceb9a4c`). The check results are in the
`ops-handoff` reply of that date. Two things to know: the production broker does not have the scorecard routes yet (BROKER-325; the route answers 404, so
VoteBot answers as before), and the log shows one traceback after `Finished server process` at shutdown (`aclose(): asynchronous generator is already
running`, from an HTTP stream closing at interpreter exit; `docker stop` took 1 s and exited 0). Later deploys are not recorded here: read the running SHA
from the ticket and `docker image ls votebot-ddp-next`. Sections 3.3, 3.5, 3.9, 5 and 6 describe this code.

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

```bash
# the scorecard routes (BROKER-325): use a real bill UUID from the resolve call above. 200 = served; 404 with a bill that resolves
# = the route is not on this broker (an unknown bill is a 404 as well, so use one that resolved)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<broker port>/api/bills/<bare uuid, lower case>/scorecard/
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<broker port>/api/legislators/<bare person uuid>/scorecard/
```

Record the broker's local URL (this becomes `DDP_BROKER_API_ROOT`; calling it over the local network instead of
the public internet is faster and removes a public dependency) and the ddp-open-states api-v3 URL and bearer token
(`DDP_OPENSTATES_API_ROOT`, `DDP_OPENSTATES_BEARER_TOKEN`; version-aware answers need api-v3's version fields). **Both are
required to start** (see 3.3).

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

**State on 2026-10-12 (reported by the old-server agent, reply `ac7e60c`):** the old VoteBot checkout
(`/home/ubuntu/votebot`) is on the branch **`legacy-webflow` at `7864abc668296f2e700e7ca081c8e65455c24032`**, clean. That commit is
the old code (`503fb99`) plus **one agreed exception to "nothing is merged": the VOTEBOT-1 widget fix** (only the `src/widget.js`
hunks of `a3a1681`: it decodes the HTML entities Webflow puts in the bill title, so `America&#39;s` shows as `America's`).
It was built without `terser` (unminified, as the file served since 2026-06-10 was), and the backend was not touched or
restarted. Still open: the **tag** `legacy-webflow-2026-10` was not created (a tag and a branch with a similar name were
judged ambiguous; say which name if it is still wanted), and the ddp-sync checkout, the unit files, `pip freeze` and the
Slack variable names from the 2026-10-07 request are not answered. Nothing from VOTEBOT-41 (link hardening) is on the old
server: its `ui.js` still has no scheme check and does not escape quotes in markdown links.

**How the old server serves the widget** (same reply): nginx (`/etc/nginx/sites-enabled/votebot`, port 80 behind Cloudflare) has
`root /home/ubuntu/votebot/chat-widget;`, so `https://votebot.digitaldemocracyproject.org/dist/ddp-chat.min.js` is the
git-tracked file `chat-widget/dist/ddp-chat.min.js` **in that checkout**, not `/var/www/votebot/` (an older copy there,
from 2026-01-31, is not used by this server block). The Webflow bill pages load that URL. The current file is the patch-only
build, sha256 `5a1b77f35aa04c03957b00646c0c74d97ca1cf3bab1027a09aca9011db19e69e` (116,582 bytes); compare the served hash
with that, **not** with a build of `main`. The bundle that was served before is kept at
`/home/ubuntu/ddp-chat.min.js.bak-20261012-0245` (sha256 `20e4d7a4...2504`). To change the widget there: commit to
`legacy-webflow`, pull on the old server, then **purge Cloudflare (everything)**. The response is
`cache-control: public, max-age=604800, immutable`, which was left as it is, so a returning visitor who does not reload can keep the
old bundle for up to 7 days (a purge does not reach browsers). Rollback of the widget: `git checkout main` in that checkout, or
copy the backup over the served path, then purge.

Rollback of the whole project is "send the website back to the old copy": it is untouched and still running.

## 3. Install the new VoteBot (new server)

Everything on this host runs in Docker and is built on the host, and the host's Python (3.9) is too old for VoteBot
(3.11 or newer), so VoteBot is a container too: **its own compose project**, defined by `infrastructure/docker/docker-compose.prod.yml` in this repo (reviewed and
versioned like the other projects here: the broker's compose file lives in its checkout too), joined to the
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
docker compose -f infrastructure/docker/docker-compose.prod.yml build      # after 3.3; watch `free -m`
```

The repo's own `infrastructure/docker/Dockerfile`, target `production` (Python 3.11-slim, non-root uid 1000, a health
check on `/votebot/v1/health/live`; about 400 MB and 75 MiB of memory at idle). Its dependencies come from
`pyproject.toml`, and a `.dockerignore` keeps `.env`, `.git` and the rest out of the build context. Do **not** use the
repo's `docker-compose.yml`: it is development only. Build in a quiet
moment after 3.1. Before building, check `free -m` (at least about 800 MB available) and `df -h /` plus `docker system df`
(at least 5 GB free); if either is short, or available memory falls below about 500 MB during the build, stop and report.

### 3.3 `/opt/votebot/.env` (git-ignored; owner root, mode 600; a fresh file, nothing copied from the old server)

```bash
ENVIRONMENT=production
API_KEY=<new random key>
OPENAI_API_KEY=<a key from a dedicated OpenAI project for this deployment>
PINECONE_API_KEY=<...>
PINECONE_INDEX_NAME=ddp-knowledge-base          # exactly this; the index name ddp-sync writes (KNOWLEDGE_BASE_INDEX_NAME)
PINECONE_NAMESPACE=<ddp-sync's PINECONE_NAMESPACE; default is "default">
DDP_BROKER_API_ROOT=<an address that serves the broker's public GETs and is not rejected as DisallowedHost; see 1.2>
DDP_OPENSTATES_API_ROOT=http://10.0.0.11:8002     # api-v3 through the host address, as ddp-sync reaches it
DDP_OPENSTATES_BEARER_TOKEN=<...>
DDP_SITE_BASE_URL=https://dev.digitaldemocracyproject.org     # production host once decided; unset is a safe no-op
ALLOWED_ORIGINS=["https://dev.digitaldemocracyproject.org"]   # JSON array; REPLACES the code default; no Webflow origins
REDIS_URL=redis://votebot-redis:6379/0
QUERY_LOG_DIR=/app/logs/queries
VOTEBOT_QUICK_ACTION_BUTTONS=true            # on from 2026-10-07 (dev site first); set in prod.env.defaults by a PR, not in the secret
DDP_OPENSTATES_AUTH_HEADER=x-api-key         # api-v3 itself accepts only X-API-Key (a Bearer token is refused with 403)
SLACK_BOT_TOKEN=
SLACK_APP_TOKEN=
```

**VoteBot will not start without `DDP_OPENSTATES_API_ROOT` and `DDP_OPENSTATES_BEARER_TOKEN`** (VOTEBOT-59,
`check_openstates_config`, the first thing the startup does). It also refuses a root whose host is `openstates.org` or any subdomain of
it (the public API: rate-limited, and without DDP's scraper corrections): VoteBot reads OpenStates only from the DDP replica, there is no
fallback and no setting for the public API. A missing, blank or public value ends the startup with a `RuntimeError` naming the variable
(`docker logs votebot-ddp-next`); with `restart: unless-stopped` the container then shows as restarting in `docker ps`. A token set to
spaces counts as missing. `DDP_BROKER_API_ROOT` is not checked at startup: left empty, the broker lookups are skipped (and ddp-next URLs get 503 from
`/content/resolve`), so check by name that it is filled. No `WEBFLOW_*` variables at all. The empty Slack lines are deliberate (one Slack connection per app token; human
handoff stays with the old copy). **Create it with `umask 077`, edit it as root (`sudo -e`), then check `stat -c '%U %a' /opt/votebot/.env` shows `root 600`. How secrets reach this file follows however ddp-sync's are provisioned on this
host; never write a key value into a note, a ticket or shell history.** The broker address warning: the broker's own
health check fails with `DisallowedHost` because Django rejects requests addressed to an internal container name, so
`DDP_BROKER_API_ROOT` cannot simply be `http://web:8000`; use the address ddp-sync uses, or have the broker's
`ALLOWED_HOSTS` extended.

#### 3.3.1 Rendering `.env` from Secrets Manager instead of typing it (VOTEBOT-14)

Like ddp-sync, the host resolves the secrets itself, so nobody types or pastes one:

```bash
cd /opt/votebot && infrastructure/render-env.sh --check    # fetch and validate; prints key NAMES only, writes nothing
cd /opt/votebot && infrastructure/render-env.sh            # writes /opt/votebot/.env (mode 600, atomic), then `up -d` to apply
```

It merges the committed non-secret defaults (`infrastructure/docker/prod.env.defaults`) with the secrets from the shared
secret `ddp-sync/credentials` (decided 2026-10-07: VoteBot shares ddp-sync's keys): `api_key`, `openai_api_key`,
`pinecone_api_key`, and `rds_openstates_api_key` (sent to api-v3 as `X-API-Key`). It fails before touching `.env` if any
is missing or empty, or has a character outside `A-Z a-z 0-9 . _ ~ + / = : @ -` (an env file cannot carry a space, quote, `#`, `$` or
backslash losslessly, so such a value is refused by name instead of being written wrong; real API keys use none of them),
and never prints a value: the secrets are fetched inside the script's Python process, never through a shell variable, so
even `bash -x` shows none. The resulting `.env` does hold the secrets in plain text (mode 600, owned by the invoking user). The host's instance role already reads this secret (ddp-sync's render
script runs the same way). To rotate: change the secret, re-run the script, `docker compose ... up -d` (a restart ends
open chats). To give VoteBot a secret of its own later: `VOTEBOT_SECRET_ID=votebot/credentials` (same key names) and
`API_SOURCE_SECRET_ID=ddp-sync/credentials` to keep the shared api-v3 key.

**After any edit to the shared secret in the console, run `infrastructure/render-env.sh --check` before leaving it.** If it
fails, correct the secret (the message names the line and column) and run it again before you leave. On 2026-10-07 four
entries were added as `"key"="value"` instead of `"key": "value"`, the secret stopped parsing, and every render failed. This
script now says where the JSON is wrong (decoder message, line and column, never a value) and leaves an existing `.env`
untouched. ddp-sync's own render script is not in this repo and was not changed; a restart or reboot of ddp-sync in that window
would have left it down (its unit renders as `ExecStartPre`). After a repair, AWS's previous version is the broken one (it was
on 2026-10-07): before rolling back to any earlier version of this secret, read it and run the check on it.

**The file is rewritten in full on every run, so a hand edit of `.env` is lost.** Non-secret settings, including the
cutover edits (`DDP_SITE_BASE_URL`, `ALLOWED_ORIGINS`), are changed in `prod.env.defaults` by a PR and re-rendered, with
a `--check` first.

**Slack, cutover day only.** The defaults leave Slack off. `infrastructure/render-env.sh --with-slack` also writes
`SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` from the secret keys `votebot_slack_bot_token` and `votebot_slack_app_token` (the bare `slack_bot_token` in the
same secret belongs to something else and is never read; confirm those two names exist in the secret with `--check --with-slack`, which prints names only; if the secret names them differently, change the
two names in the `--with-slack` lines of the script by a PR). One Slack app token may be held by ONE running VoteBot, so:
stop the old copy's Slack connection first and **confirm it is stopped before going on** (its service or process is
down, or its Slack app shows no connection; record the time), then render with `--with-slack`, `up -d --force-recreate votebot`, and run a real
human-handoff test (VOTEBOT-14).

### 3.4 The compose project

`infrastructure/docker/docker-compose.prod.yml` (project name `votebot-ddp-next`): the `votebot` container
(`votebot-ddp-next`, built from the Dockerfile above, `mem_limit` 1g (see 3.8), `env_file` `/opt/votebot/.env`, a bind mount
`${VOTEBOT_LOG_DIR:-/opt/votebot-logs}` at `/app/logs`, joined to the broker's Docker network `ddp-broker-py_default`) and
`votebot-redis` (128 MB dataset cap inside a 256 MB container limit, no persistence). The log directory must exist and be owned by uid 1000:
`sudo install -d -o 1000 -g 1000 /opt/votebot-logs/queries`. Deploying is the same routine as the other projects:
`cd /opt/votebot && git pull --ff-only`, `docker compose -f infrastructure/docker/docker-compose.prod.yml build`, then `... up -d`.

**Preflight, before the first `up`** (each answers yes/no; stop on a no): no container already named `votebot-ddp-next` or
`votebot-redis` (`docker ps -a --filter name=votebot`); the network exists (`docker network ls | grep ddp-broker-py_default`);
`/opt/votebot/.env` exists, is mode 600, and has every key filled (check by name only: `grep -c '^NAME=.' /opt/votebot/.env`;
include `DDP_OPENSTATES_API_ROOT`, `DDP_OPENSTATES_BEARER_TOKEN` and `DDP_BROKER_API_ROOT`: without the first two VoteBot does not start, 3.3);
`/opt/votebot-logs/queries` exists and is owned by uid 1000, and a write as that user works
(`sudo -u '#1000' touch /opt/votebot-logs/queries/.t && sudo rm /opt/votebot-logs/queries/.t`); `free -m` shows at least
about 600 MB available and `df -h /` has room. VoteBot also logs an ERROR at startup ("Query log directory is NOT writable")
if the log directory is wrong, but it keeps answering: look for that line.

**Rollback tag.** `build` overwrites the image `votebot-ddp-next:local`, so before every rebuild tag the running one with its
commit: `docker tag votebot-ddp-next:local votebot-ddp-next:<sha of the running commit>`. Rolling back is then: retag that
image as `:local` and run `up -d --no-build votebot` (so Compose uses the retagged image instead of rebuilding), or `git checkout <previous sha>`,
rebuild and `up -d`. Before the first rebuild, confirm the previous tag exists: `docker image ls votebot-ddp-next`. Record the known-good SHA in the ticket.
The env file is read by the compose file itself (`env_file: ../../.env`, resolved relative to the compose file, so `/opt/votebot/.env` when the
repo is at `/opt/votebot`); no `--env-file` flag is needed, and no command needs `docker compose config`, which would print the secrets.

VoteBot has a Redis of its own (never the broker's, never the host's 6379): a button cache, a handoff map and chat
sessions (2 h) live in it, so no persistence. The image's command runs `WEB_CONCURRENCY` workers (1 today, set in
`prod.env.defaults`; see 3.8). A session is kept in Redis as well as in the worker's memory, so a visitor who reconnects
to another worker, or after a restart, finds the history and a handoff in progress (a Redis restart loses them: the
widget then starts a fresh chat).

### 3.5 Start it and verify it INSIDE the Docker network, before any nginx change

(Redis restarting or evicting keys loses only cached quick-action answers, which are regenerated on demand, and open
human-handoff thread mappings; chat history lives in VoteBot's own memory and is lost only when VoteBot restarts.)

```bash
docker compose -f infrastructure/docker/docker-compose.prod.yml up -d
docker logs votebot-ddp-next 2>&1 | grep -E 'VoteBot started|PINECONE_INDEX_NAME|Traceback|Error'
docker exec votebot-ddp-next python -c "import aiofiles, websockets, httpx; print('deps ok')"
docker exec votebot-ddp-next python -c "import httpx; print(httpx.get('http://localhost:8000/votebot/v1/health/ready').text)"
```

The startup line must show `pinecone_index_name=ddp-knowledge-base` and `bill_filter_key=ocd_bill_id`, with no warning
about an unrecognised index (a wrong name makes Pinecone create an EMPTY index: confirm the index exists with vectors
in the namespace first, read-only, as in section 1.2's check). Then section 6, run from inside the container.

**Startup, what to expect (VOTEBOT-49, 59).** Startup stops at once, before Redis is touched, if the OpenStates settings are missing or
point at the public host (3.3). Otherwise the `VoteBot started (chat-only mode)` line appears without waiting for anything slow: the
warm-up of Pinecone (the index is opened and one `describe_index_stats` request made; it only reads, and never creates a missing
index) and of the embedding client (one tiny embedding) runs in the background, so an unreachable Pinecone or OpenAI cannot delay or
stop the start. Its outcome is one more line a few seconds later: `Warm-up finished`, or `Warm-up incomplete` / `Warm-up failed` with
`failed=[...]` naming `pinecone` and/or `embeddings` and `pinecone_index_found`. An incomplete warm-up is not an outage (the first chat
then pays for what did not warm), but `pinecone_index_found=False` is the cue to re-check the index name above. The warm-up is
cancelled if the container stops before it ends. The federal party roster is also fetched in the background at start (VOTEBOT-48), so
the first legislator question does not wait for it. `docker logs votebot-ddp-next 2>&1 | grep -E 'Warm-up|started'`.

**Which health URL for what (VOTEBOT-51).** `/votebot/v1/health/live` answers `alive` and checks nothing: it is what a container or
orchestrator health check should call, and what the image's `HEALTHCHECK` calls (it fails only if the process does not answer). `/votebot/v1/health/ready` checks
Pinecone, the OpenAI model, the OpenStates replica's `/healthz` and Redis, and returns `degraded` with each dependency named
(`pinecone`, `openai`, `ddp_openstates_replica`; `redis` shows `unavailable` and does not degrade). Its model check is a real completion
of 16 tokens, so it proves the key can generate, but it is **made at most once a minute**: a healthy result is kept for 60 s, a failure
for 10 s (so a recovery shows soon), and checks that arrive together share one call. Do not point a monitor that fires every few
seconds at `/ready` expecting a fresh model call each time, and do not use it as the container health check; use it by hand,
after a deploy and in section 6. Pinecone, the replica and Redis are checked on every `/ready` call.

### 3.6 Logs

Query logs are one JSONL file per day in `/opt/votebot-logs/queries` (visitors' messages and addresses). They are personal
data, so they are aged out by `infrastructure/query-log-cleanup.sh` (VOTEBOT-20; decision 2026-10-07: a year at most): files
older than 7 whole days are compressed (a file is compressed once it is 8 days old), files older than 365 whole days are
deleted (at 366 days); `COMPRESS_DAYS`, `DELETE_DAYS`, `LOG_DIR` override the defaults. A failed compress or delete is
printed and makes the script exit 1, which cron mails to the operator. The weekly quality report reads the last 7 days uncompressed. A plain logrotate rule does not fit: the files
are already named by day. The operator installs it (needs sudo; a name with a dot is skipped by `run-parts`, so no `.sh`):

```bash
cd /opt/votebot
sudo infrastructure/query-log-cleanup.sh --dry-run          # lists what it would compress and delete; changes nothing
sudo install -m 755 infrastructure/query-log-cleanup.sh /etc/cron.daily/votebot-query-log-cleanup
sudo /etc/cron.daily/votebot-query-log-cleanup              # one real run, then:
ls -l /opt/votebot-logs/queries | tail -5                   # today's file is still there and its size still grows
docker logs votebot-ddp-next 2>&1 | grep -c 'Query log directory is NOT writable'    # expect 0
```

Container logs are rotated by the `json-file` options in 3.4. Check `df -h /` after the first week and name who looks.

Slow answers are looked at in 3.9, from the same log files.

### 3.7 nginx: the broker's, through the broker repo (PR #410)

The broker's nginx container renders its template when it starts. VoteBot's two locations (`/ws/chat`, `/votebot/`) are
in `ddp-broker-py` PR #410, written with the broker's own patterns: the upstream address is a variable (BROKER-115), so
nginx starts without VoteBot and finds a recreated container again; and the locations declare their own `add_header` so
the wide-open CORS header set at the top of that server is not added a second time to VoteBot's responses (two
`Access-Control-Allow-Origin` headers make browsers reject them). Apply it **only after VoteBot is verified in 3.5 and
the PR is merged**, in a quiet window:

1. Test the merged template in a throwaway container with the production nginx service's image, mounts and environment:
   `docker run --rm <same mounts and -e flags> nginx:stable-alpine nginx -t` must report "test is successful".
2. Recreate only the nginx container, in the broker's own project: `docker compose up -d --no-deps --force-recreate nginx` (never the whole
   project; a few seconds of broker downtime), then repeat the baseline health checks and check the broker's own public routes. Before the
   recreate, confirm the new template renders in the real container's network: `docker exec <nginx container> getent hosts votebot-ddp-next`
   resolves once VoteBot is running. If any check fails, restore the previous template and recreate again.
3. Public checks: `curl -s https://mapapp.digitaldemocracyproject.org/votebot/v1/features` and the smoke test against
   `wss://mapapp.digitaldemocracyproject.org/ws/chat`.

Rollback: revert the broker change and recreate nginx; VoteBot keeps running untouched. No DNS or certificate work is
needed: `mapapp.digitaldemocracyproject.org` and its certificate already exist.

### 3.8 How many workers

**Today: 1** (`WEB_CONCURRENCY=1` in `infrastructure/docker/prod.env.defaults`). Measured on 2026-10-11 (VOTEBOT-34) with
`scripts/load_ws.py`: the real app with only the LLM answer stubbed (so no LLM cost, and a floor of about 0.8 s per answer),
Pinecone, embeddings and api-v3 real, clients connecting over 3 s, each asking a quick-action button, a typed question and a
"what changed" question. p50 seconds per typed question, and the longest `/health/live` took while loaded:

| clients | before the fixes, 1 worker | after VOTEBOT-36 and 37, 1 worker |
|---|---|---|
| 10 | 4.1 s, longest health check 2.4 s | 0.8 s, 0.1 s |
| 25 | 8.8 s, 4.7 s | 0.8 s, 0.1 s |
| 50 | 18.1 s, 17.1 s | 0.8 s, 0.1 s |
| 100 | 32.1 s, 26.5 s, 6 of 300 questions failed (sockets dropped) | 1.1 s, 0.1 s, none failed |

The cause was the Pinecone client, which is synchronous and ran on the event loop (VOTEBOT-36), so every chat waited for
every other one while the CPU sat at about 14%; adding workers hid it only by dividing the queue. Peak memory of all
the processes at 100 simultaneous chats, with VOTEBOT-36, 37 and 38 (stubbed LLM; a real answer adds waiting, not CPU):

| workers | peak MiB | p50 / p95 typed | cpu % of one core (sum) |
|---|---|---|---|
| 1 | 234 | 1.4 / 2.7 s | 38 |
| 2 | 421 | 1.3 / 2.7 s | 42 |
| 3 | 559 | 1.2 / 2.8 s | 53 |

So one worker holds 100 simultaneous chats with room to spare, and the container limit is 1 GiB (it was 768 MiB: the old
code reached 546 MiB at 100 chats). More workers add resilience (one process dying does not drop every chat) and a second
core, at about 140 to 190 MiB each; they do not add speed at this size. **Before deploying the memory limit or any count above 1,** the checkout must contain VOTEBOT-36, 37 and 38 (`git log --oneline | grep -E 'VOTEBOT-3[678]'` shows all three); the figures above depend on them. **To change the count:** a PR editing
`WEB_CONCURRENCY` in `prod.env.defaults`; then `infrastructure/render-env.sh --check`, `render-env.sh`, and
`up -d --force-recreate votebot` (check there are no open chats first, and tag the running image as in 3.4). Afterwards, check that the setting arrived and took effect:
`docker exec votebot-ddp-next printenv WEB_CONCURRENCY` (the rendered `.env` reaches the container through `env_file`),
`docker logs votebot-ddp-next | grep -c "Started server process"` (one line per worker),
`docker inspect -f '{{.HostConfig.Memory}}' votebot-ddp-next` (1073741824 for 1g) and `docker stats votebot-ddp-next` (the
memory above); then run the smoke test of section 6. To go back: revert the PR, `render-env.sh`, and recreate (or retag the
previous image, see "Rollback tag" in 3.4). Re-run the load test (below) before going beyond 3.

The two tables are separate runs (the second includes VOTEBOT-38's Redis saves, and a run varies by a few tenths of a
second: 1.1 s and 1.4 s for 100 clients on one worker are the same result). Run it in dev, never against production:
`python scripts/load_ws.py --workers 1 2 3 --clients 10 25 50 100`.

**Audit of what is per-process (the prod agent's note of 2026-10-11, checked against the code):**

| Item | Result |
|---|---|
| Websocket session state (history, handoff flag, counters) in each worker's memory | Was the blocker. Fixed (VOTEBOT-38): saved to Redis after every client message and Slack event, loaded on connect; a save never overwrites a copy with a later message. Tested by simulating a worker change; with the Slack handoff on, do the real handoff test (3.3.1, VOTEBOT-14) at the worker count that will be live |
| Slack replies reaching the worker that holds the visitor's socket | Fine as designed: every worker subscribes to the Redis channel and only the worker holding the socket delivers; the thread-to-session map is in Redis. Each worker that has served a visitor opens its own Socket Mode connection with the same app token (Slack sends each event to one of them) |
| An agent, with its Pinecone/OpenAI/httpx clients and caches, built for every message | Fixed (VOTEBOT-37): one set per process; the api-v3 version cache and the party cache now work across messages |
| Blocking calls on the event loop | The Pinecone client was the one. Fixed (VOTEBOT-36). OpenAI, httpx, Redis, Slack and the log writes are async. Left: Pinecone `upsert`/`delete`, which only ddp-sync and scripts use |
| Federal legislator cache "loaded over HTTP by every worker" | Not so: it is read from `data/cache/federal_legislators.json` (the file is excluded from the image by `.dockerignore`, so the cache is empty here, and on the canonical index what it finds is not used) |
| `MetricsCollector` per worker | Unused; nothing reads it |
| `reconcile_on_startup` in every worker | Fine: it only deletes button-cache entries saved before the bill's last check, so one worker cannot delete another's fresh entry, and it matches slug-keyed entries only |
| Several processes appending to one daily query-log file | Fine: 4 processes each writing 100 lines of 20 KB at once gave 400 intact lines (appends, one per line) |
| In-memory rate limiter | None exists |

### 3.9 When answers are slow: the timing fields and the latency report (VOTEBOT-44, 45, 47, 53)

Every streamed answer writes a `query_processed` event to the query log (3.6; in the container `/app/logs/queries`, on the host
`/opt/votebot-logs/queries`) with `duration_ms` and, since VOTEBOT-44, `first_token_ms`, `marks_ms` and `phase_ms`. The same numbers are in the
container log as a `Streaming timings` line. Only the streaming path is instrumented (the REST `POST /chat` is not), and
the fields are absent from older lines.

```bash
cd /opt/votebot && python3 scripts/latency_report.py --log-dir /opt/votebot-logs/queries --days 7     # stdlib only: no network, no keys
docker exec votebot-ddp-next python scripts/latency_report.py --log-dir /app/logs/queries --days 7     # the same, inside the container
python3 scripts/latency_report.py --log-dir /opt/votebot-logs/queries --days 7 --json > report-<sha>.json
```

It prints p50 / p90 / p95 with the sample size for `duration_ms`, `first_token_ms`, `model_call_start` and every phase, cut by page kind,
live bill lookup, button and saved-answer hit, and says whether the README targets are met (p50 under 2.5 s and p95 under 5 s end to end,
first token p50 under 1.5 s; `first_token_ms` is measured on the server, so the visitor waits a little longer). A cut with fewer than 20
messages is marked `(small sample)`: do not draw a conclusion from it. `--skip-button NAME` leaves out one quick-action button.
Keep one report from before a change and one from after, with the deployed SHAs.

**Read `first_token_ms` and `marks_ms.model_call_start`, not the sum of the phases.** Since VOTEBOT-45 and 47 the retrieval queries and the
pre-model lookups run at the same time, so phase durations overlap and add up to more than anyone waited. `model_call_start` is the wait
before the model was even asked (the slowest of retrieval and the lookups); `first_token` minus it is the model's own time to its first
text. What each `phase_ms` key times (`core/agent.py`):

| `phase_ms` key | What it is | When it is present |
|---|---|---|
| `button_cache` | the lookup of a saved quick-action answer in Redis | every streamed message; a hit ends the message here (no other phase) |
| `retrieval` | all Pinecone work for the message, and the embedding of the query | every miss |
| `bill_info_prefetch` | the live bill lookup in api-v3 (status, votes) | a vote/status question or a bill named in the message, or low retrieval confidence |
| `legislator_lookup` | the legislator name lookup in api-v3 (and the party roster) | every message on the canonical-id index |
| `vote_verification`, `webflow_verification` | verification of a disputed claim | a message detected as a dispute or correction |
| `org_positions`, `org_bill_positions` | organization positions from the broker | a bill page asking who supports or opposes; an organization page asking which bills |
| `bill_broker_data` | LegBot's analysis of the bill, the concept poll and the bill's scorecard, from the broker | a bill page that names the bill (the poll and the scorecard only when the question asks) |
| `legislator_scorecard` | the legislator's scorecard from the broker | a legislator page whose `id` is `ocd-person/<uuid>`, and a question about votes against constituents |

How to read it. A high `model_call_start_ms` with one large phase names the slow dependency: `retrieval` (Pinecone or OpenAI embeddings: check `/health/ready`),
`bill_info_prefetch` or `legislator_lookup` (api-v3, the replica), or a broker phase. Broker and legislator lookups share one
**8-second budget per message**, a single broker request waits at most 5 s, and a lookup that fails or runs out of time costs only its
own context (a warning in the log, `Broker lookup took too long` or `failed`), so a broker phase near 5 to 8 s means the broker is slow or down and the
answer went out without it. A small `model_call_start_ms` with a late `first_token` is the model. If only the first message after a
start is slow, look for `Warm-up incomplete` (3.5): what did not warm is paid for by that message. The party roster (federal) is served stale while it refreshes, so only a never-seen
jurisdiction waits for a download.

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
cd /opt/votebot-logs/queries
jq -r 'select(.event_type=="message_received") | .client_ip' $(date +%F).jsonl | sort | uniq -c | sort -rn | head
```

Slow answers are the other thing to look at weekly: run the latency report in 3.9 and compare it with last week's.

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

**What this section needs from code.** `scripts/smoke_ws.py` (VOTEBOT-16, in `main`). Links to our own `/bill/...` pages need `DDP_SITE_BASE_URL` and organization positions from the broker need `DDP_BROKER_API_ROOT`; both are VOTEBOT-15 part 2 (PR #16, merged). Legislator answers and "what changed" read api-v3 live (VOTEBOT-15 part 3, PR #17) and need the DDP replica (`DDP_OPENSTATES_API_ROOT`, which VoteBot now requires). Deploy a version of `main` that contains all of them before running this section, or the checks below will fail for the right reason. The startup line to check (`docker logs votebot-ddp-next | grep 'VoteBot started'`) is a structured log that includes `pinecone_index_name=ddp-knowledge-base` and `bill_filter_key=ocd_bill_id` (`src/votebot/main.py`).

```bash
# first INSIDE the Docker network (before the nginx change), then through nginx
docker exec votebot-ddp-next python scripts/smoke_ws.py --url ws://localhost:8000/ws/chat --cases scripts/smoke_cases.json --retrieval
docker exec votebot-ddp-next python scripts/smoke_ws.py --url wss://mapapp.digitaldemocracyproject.org/ws/chat --cases scripts/smoke_cases.json
```

(the image contains `scripts/` and the container has its `.env`, which `--retrieval` needs; the README's "WebSocket Smoke Test" explains
the checks; the canonical index must already contain embedded bills, since Pinecone creates a missing index on
first use and an empty one answers nothing). It prints the index and namespace it read; confirm they are
`ddp-knowledge-base` and the namespace the new ddp-sync writes, and that an answer never cites old-index content.

**Scorecard answers (VOTEBOT-60), by hand, once the broker serves the routes (1.2).** On a bill page whose `page_context` carries a
bare `ocd_bill_id` (the websocket drops any other spelling to null), ask "do the legislators' votes line up with their constituents?":
the answer should speak of DDP's constituent-alignment scorecard (a vote's district choice is the majority of that district's
constituents on DDP). A legislator page needs `id` = `ocd-person/<uuid>` and a question such as "how often does she vote against her
district?"; ddp-next sends only the name today (NEXT-77), so this check cannot pass from the site yet. Without the routes, or without
those ids, VoteBot answers as before from the vote record and the profile, with no error: check the log for `Broker lookup failed` with
the scorecard `path`. Only a question about alignment fetches a scorecard (`accountab*`, `align*`, `scorecard`, "in line with", or
voters/constituents/district next to a word about voting).

Failure paths, once, before cutover: stop the broker briefly in a quiet moment (or point `DDP_BROKER_API_ROOT` at a
closed port and restart) and confirm answers still come, just without organization positions; restart VoteBot in
the middle of a chat and confirm the widget reconnects; reconnect with the **same session id** right after a close (gaps of 0, 50, 200 and 1000 ms) and confirm the answer comes back each time (VOTEBOT-42: a closing connection must not remove a newer one); send an `/content/resolve` request from an origin that is
not allowed and confirm it is refused; confirm `docker logs votebot-ddp-next` shows no Slack or Webflow activity. A small load check:
run the smoke script from a few terminals at once and watch memory, CPU, `df` and the broker's response time; abort
if the broker slows noticeably. Then, in a browser on the dev origin: open a bill page with the chat,
ask a question, and confirm the answer links to our own `/bill/...` page, the "who supports" question lists
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
  infrastructure/docker/docker-compose.prod.yml down`, revert the broker nginx change and recreate nginx (3.7).

## 8. Checklist for the ticket

- [ ] 1.1 to 1.3 filled in, differences reported
- [ ] Section 2: `legacy-webflow` branch and tag in votebot and ddp-sync, old checkouts pinned, paths recorded (votebot: branch done at `7864abc`, no tag; ddp-sync: not done)
- [ ] 3.1 swap added; 3.2 to 3.5: image built on the host, container running and verified INSIDE the Docker network (`bill_filter_key=ocd_bill_id` in the startup log, no Slack), own Redis container, memory limits in place, query-log cleanup installed (3.6)
- [ ] 3.7: broker PR #410 merged, template tested in a throwaway container, nginx recreated in a quiet window, public smoke test passing
- [ ] Baseline health table filled in and unchanged after each step
- [ ] 4.1 production origin supplied and added at cutover; 4.2 widget path documented in `chat-widget/README.md`
- [ ] Section 5: weekly monitoring owner named; OpenAI usage limit and alert set; Turnstile or a proxied hostname only if needed
- [ ] 1.2: the scorecard routes answer on the broker the host points at (BROKER-325; not on production as of 2026-10-27), or the gap is written down: answers degrade, nothing breaks
- [ ] Section 6 verified with real bills, failure paths and the small load check done; rollback rehearsed on the dev site
- [ ] Hand back to NEXT-36 (which `wsUrl`) and SYNC-92
