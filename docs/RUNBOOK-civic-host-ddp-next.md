# Runbook: running VoteBot for the new website (ddp-next) on the new-infrastructure server

Ticket: VOTEBOT-14. Blocks NEXT-36 (chat on the new site) and SYNC-92 (the cutover).
Run it through the prod agent (`ddp-broker-py` `notes/ops-handoff`). **Nothing in this document has been run.**
It was written from the repos only: the live nginx configs and real `.env` files were not available, so every
step starts with a read-only check and every value that is not in a repo is marked `<...>`, not guessed.

## 0. The layout (decided by Ramon, 2026-10-05)

| | Old VoteBot (Webflow) | New VoteBot (ddp-next) |
|---|---|---|
| Server | the existing civic server | the new-infrastructure server: ddp-broker-py, ddp-open-states, ddp-sync (new), and this |
| Code | frozen on the branch `legacy-webflow`, nothing merged into it | `main`, with Webflow removed |
| Index | `votebot-large` | `ddp-knowledge-base` |
| Data pipeline | the old ddp-sync, also frozen on a branch | the new ddp-sync on the new server |
| Slack handoff | stays here | **off** until the new copy gets its own Slack setup (only one copy may hold the Slack connection) |
| Webflow | still used | **none at all** |

Decisions recorded:

- **Second copy of VoteBot, built for the new infrastructure only: yes.** There is no second copy on the old
  server and no `/next` path. (The earlier plan for that is gone.)
- **Rate limiting of connections: no, not now.** Monitor first (section 5). Bot protection for suspicious
  traffic, without affecting real visitors, is section 5 (Cloudflare first, an invisible check later if needed).
- **Answers link to our own pages**: `DDP_SITE_BASE_URL` (VOTEBOT-15 part 2).
- **Organization positions from our own data**: ddp-broker-py (VOTEBOT-15 part 2).

What the new VoteBot does to the broker, so the shared host is not at risk from it: only public, unauthenticated
**GET** reads (`/api/bills/resolve/`, `/api/bills/{id}/scorecard/`, `/api/bill-organization-positions/current/`,
`/api/organizations/{id}/` and `/positions/`). It holds no broker credential, writes nothing, and starts no job.

Order of work: section 1 (read-only), section 2 (freeze the old copy), section 3 (install the new one),
section 4 (the website and its origins), then 5 (bot protection and monitoring), 6 (verify), 7 (cutover and rollback).

**Ground rules for every change on a host.** Back up first, with a timestamp, and note the backup in the ticket
(`sudo cp -a <file> <file>.bak-$(date +%Y%m%d-%H%M)`). Run `sudo nginx -t` before every
`sudo systemctl reload nginx`, and afterwards re-check that the routes that worked before still do. Abort and
roll back (restore the backups, reload/restart, repeat the earlier checks) if a route that worked changes
status, a restarted service does not come back within a minute, free memory drops as in 1.1, disk use grows
unexpectedly, or an answer comes from the wrong index. The prod agent may abort at any point and should report
what was and was not changed.

**Baseline health of everything else on the server.** Before the first change, write down that these answer
(whatever the real checks are for the broker, api-v3/ddp-open-states and ddp-sync: a status endpoint or a `curl`
and the `systemctl` state of each) and repeat them after installing Redis, after starting VoteBot, after every
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
systemctl list-units --type=service --state=running | grep -E 'broker|openstates|sync|redis|nginx|votebot'
ps -eo pid,rss,cmd --sort=-rss | head -12                  # the biggest memory users right now
```

Expected from what Ramon described: the box is mostly ddp-sync starting Fargate jobs, so it is largely idle; the
one heavy job is scorecard creation, which is moving to Fargate. VoteBot is small (it mostly waits on OpenAI,
Pinecone and the broker). If free memory is thin **while a scorecard run is going**, either wait for that job to
move or add memory; do not guess.

| Check | Found |
|---|---|
| free memory and swap, idle and during a scorecard run |  |
| a free local port for VoteBot (this runbook uses `8002`; 8000/8001 are used elsewhere in the fleet) |  |
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

### 3.1 Code and user

VoteBot is the only part of this server that takes anonymous input from the public, so run it as its own user
with nothing else in reach:

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin votebot
sudo -u votebot git clone git@github.com:Digital-Democracy-Project/votebot.git /home/votebot/votebot
cd /home/votebot/votebot && sudo -u votebot python3 -m venv venv && sudo -u votebot venv/bin/pip install -e .
```

Deploy from `main` once the Webflow removal has merged. Until then, `main` plus the settings below works (the
Webflow code is simply never called on this index).

### 3.2 Its own Redis

A separate `redis-server` process (its own systemd unit, data directory and port), never the broker's instance
and never a database number on it, so the broker's jobs and VoteBot cannot affect each other and nothing here can
flush the broker's data. VoteBot keeps only a button cache and the handoff thread map in it, so no persistence
is needed:

```conf
# /etc/redis/votebot.conf
bind 127.0.0.1
port 6380
save ""
appendonly no
maxmemory 256mb
maxmemory-policy allkeys-lru
```

`REDIS_URL=redis://127.0.0.1:6380/0`. It listens on the local address only, so no password is needed. Do not run
any `redis-cli` command against port 6379.

### 3.3 `/home/votebot/votebot/.env` (mode 600, owner `votebot`)

```bash
ENVIRONMENT=production
API_KEY=<new random key>
OPENAI_API_KEY=<...>
PINECONE_API_KEY=<...>
PINECONE_INDEX_NAME=ddp-knowledge-base        # exactly this name; anything else keeps the legacy mode and logs a warning
PINECONE_NAMESPACE=default                    # confirm it matches what the new ddp-sync writes
DDP_BROKER_API_ROOT=<from 1.2>
USE_DDP_OPENSTATES_REPLICA=true
DDP_OPENSTATES_API_ROOT=<from 1.2>
DDP_OPENSTATES_BEARER_TOKEN=<from 1.2>
DDP_SITE_BASE_URL=<the new site's production URL once decided; dev URL until then; unset = links stay as they were>
ALLOWED_ORIGINS=["https://dev.digitaldemocracyproject.org"]   # JSON array; add the production origin at cutover (4.1)
REDIS_URL=redis://127.0.0.1:6380/0
QUERY_LOG_DIR=/home/votebot/votebot/logs/queries
VOTEBOT_QUICK_ACTION_BUTTONS=<as on the old server>
SLACK_BOT_TOKEN=
SLACK_APP_TOKEN=
```

No Webflow keys or collection ids at all. Slack is left empty on purpose (one Slack connection per app token).
This is a fresh `.env`; nothing here is inherited from the old server's file.

### 3.4 Service

```ini
# /etc/systemd/system/votebot.service
[Unit]
Description=VoteBot (ddp-next)
After=network.target

[Service]
User=votebot
WorkingDirectory=/home/votebot/votebot
Environment=PYTHONPATH=src
EnvironmentFile=/home/votebot/votebot/.env
ExecStart=/home/votebot/votebot/venv/bin/uvicorn votebot.main:app --host 127.0.0.1 --port 8002 --workers 1
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/home/votebot/votebot/logs
MemoryMax=1G
TasksMax=200
LimitNOFILE=4096

[Install]
WantedBy=multi-user.target
```

**One worker, deliberately.** A session's chat history is kept in the memory of the worker that served it; with
two workers a reconnect that lands on the other one restores nothing (noted on VOTEBOT-15). One worker is plenty
for this load, and it keeps conversations intact.

`EnvironmentFile=` without a leading `-` makes the unit refuse to start if the file is missing, so a missing
`.env` fails closed. The working directory is VoteBot's own, so the only `.env` it can ever read is its own.
`MemoryMax` bounds what a problem in VoteBot can take from the broker's server; check it is not too tight when
you test (section 6). Restarting the service ends open chats and their history (it is kept in the worker's memory):
that is expected, and the widget reconnects.

```bash
sudo mkdir -p /home/votebot/votebot/logs && sudo chown votebot: /home/votebot/votebot/logs
sudo systemctl daemon-reload && sudo systemctl enable --now votebot
journalctl -u votebot -n 40 --no-pager | grep -E 'VoteBot started'     # pinecone_index_name=ddp-knowledge-base, bill_filter_key=ocd_bill_id
curl -s http://127.0.0.1:8002/votebot/v1/health/ready
```

### 3.5 Logs

VoteBot writes one JSONL file per day under `QUERY_LOG_DIR`, including visitors' messages and addresses. Keep
them 14 days and compress, so they neither fill the shared disk nor keep people's messages longer than needed:

```conf
# /etc/logrotate.d/votebot
/home/votebot/votebot/logs/queries/*.jsonl {
    daily
    rotate 14
    compress
    missingok
    notifempty
    su votebot votebot
}
```

Check `df -h /` after the first week and name who looks at it.

### 3.6 nginx (new hostname, behind Cloudflare)

A new subdomain for VoteBot on this server (name to be chosen: `<votebot-host>`), proxied to `127.0.0.1:8002`.
Before editing: back up the config, check the name is not already used
(`sudo nginx -T | grep -n server_name | grep <votebot-host>`), and check the certificate covers it. Use
`nginx -t` and then `reload` (never `restart`), and repeat the baseline health checks of section 0 straight after.
There are **no connection limits** for now (decision above).

```nginx
server {
    server_name <votebot-host>;
    # listen/ssl lines as for the other hostnames on this server

    location /ws/chat {
        proxy_pass http://127.0.0.1:8002;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 86400;
    }
    location /votebot/ {
        proxy_pass http://127.0.0.1:8002;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

If Cloudflare is in front, the logged client address is Cloudflare's unless nginx restores the real one
(`set_real_ip_from` for each current Cloudflare range, IPv4 and IPv6, from cloudflare.com/ips, and
`real_ip_header CF-Connecting-IP;`). `set_real_ip_from` only believes the header when the connection comes from
those ranges, so a visitor reaching the server directly cannot spoof it; still check whether the server can be
reached directly at all (if it can, restrict port 443 to Cloudflare's ranges in the security group). Refresh the
ranges when Cloudflare publishes changes. Do this now: section 5's monitoring depends on it.

## 4. The website

### 4.1 Allowed origins

Only the HTTP calls (`/content/resolve`, `/features`) are subject to the browser's origin check; the socket is
not, and VoteBot has no origin check on it. `ALLOWED_ORIGINS` on the new copy lists only new-site origins
(`https://dev.digitaldemocracyproject.org` now). **At cutover add the new site's production origin: exact scheme
and host, to be supplied by NEXT-36 / Ramon.** Webflow origins are not listed here; they belong to the old copy.

### 4.2 The chat widget bundle

The widget file is the same for both copies. Find where ddp-next serves static files (1.2) and where the bundle
should live there; record the answer and the copy procedure in `chat-widget/README.md` ("How a change reaches
production"). After every copy, purge the Cloudflare cache for `ddp-chat.min.js`, and check the served file's
hash equals the repo's (`sha256sum chat-widget/dist/ddp-chat.min.js` against `curl -s <url> | sha256sum`).

The new site configures the widget with the new copy:

```javascript
window.DDPChatConfig = { wsUrl: 'wss://<votebot-host>/ws/chat', pageContext: { type: 'bill', id: 'HB 219',
  jurisdiction: 'FL', session: '2026', ocd_bill_id: '<uuid>' } };
```

(or only a `?ddp_url=` link, which `/content/resolve` turns into the same thing). The address lives in the new
site's own page configuration, not in the bundle (the bundle's built-in default is the old production address
and only applies when a page sets none). So moving users, and moving them back, is a change to ddp-next's
configuration and its deploy. Find out how long that takes and write it down: a tab that already has the chat
open keeps its old connection until it is reloaded.

## 5. Bot protection and monitoring (no limits now)

There is no limit on opening chat connections, by decision. Every chat message costs OpenAI and Pinecone calls,
so watch for abuse and have a plan.

**Watch (a few minutes a week at first).** Messages per visitor address, from VoteBot's own logs (needs the
real-IP step in 3.6; check the field name with `head -1` first):

```bash
cd /home/votebot/votebot/logs/queries
jq -r 'select(.event_type=="message_received") | .client_ip' $(date +%F).jsonl | sort | uniq -c | sort -rn | head
```

Look for one address sending hundreds of messages, many short sessions from one address, or identical
messages. Also set a **monthly usage limit and an email alert in the OpenAI dashboard**: it is the one real
backstop for cost, needs no code, and works whatever the cause. Give the new VoteBot **its own OpenAI project
and API key** so the limit applies to it alone, and find out whether reaching the limit only alerts or
actually blocks requests (and which other products would be affected if the key were shared).

**If bots show up, in this order:**

1. **Cloudflare's built-in bot filtering on the VoteBot hostname (no code).** It scores each visitor and
   challenges only automated-looking ones; people never see anything. Turn on Bot Fight Mode (or the
   equivalent managed bot rules on this plan) for `<votebot-host>`, then check **in a browser** that the chat
   still opens and the socket connects, and that the new website's own calls to VoteBot are not challenged
   (add a skip rule for them if they are). A challenge cannot appear in the middle of an open socket, which
   is why the check has to happen before the chat opens.
2. **Cloudflare Turnstile (a small code change).** An invisible check that runs when the chat opens: free,
   and only suspicious visitors ever see a prompt. The widget would obtain a token and VoteBot would verify it
   before accepting the connection. Only worth building if step 1 is not enough; open a ticket then.
3. A per-visitor cap on messages (application code) only if an abuser opens one connection and floods it.

## 6. Verify

**What this section needs from code.** `scripts/smoke_ws.py` (VOTEBOT-16, in `main`). Links to our own `/explore/...` pages need `DDP_SITE_BASE_URL` and organization positions from the broker need `DDP_BROKER_API_ROOT`; both are VOTEBOT-15 part 2 (PR #16, merged). Legislator answers and "what changed" read api-v3 live (VOTEBOT-15 part 3, PR #17) and need `USE_DDP_OPENSTATES_REPLICA=true`. Deploy a version of `main` that contains all of them before running this section, or the checks below will fail for the right reason. The startup line to check (`journalctl -u votebot | grep 'VoteBot started'`) is a structured log that includes `pinecone_index_name=ddp-knowledge-base` and `bill_filter_key=ocd_bill_id` (`src/votebot/main.py`).

```bash
python scripts/smoke_ws.py --url wss://<votebot-host>/ws/chat --cases <real cases> --retrieval
```

(run it on the new server with its `.env` loaded for `--retrieval`; the README's "WebSocket Smoke Test" explains
the checks; the canonical index must already contain embedded bills, since Pinecone creates a missing index on
first use and an empty one answers nothing). It prints the index and namespace it read; confirm they are
`ddp-knowledge-base` and the namespace the new ddp-sync writes, and that an answer never cites old-index content.

Failure paths, once, before cutover: stop the broker briefly in a quiet moment (or point `DDP_BROKER_API_ROOT` at a
closed port and restart) and confirm answers still come, just without organization positions; restart VoteBot in
the middle of a chat and confirm the widget reconnects; send an `/content/resolve` request from an origin that is
not allowed and confirm it is refused; confirm the journal shows no Slack or Webflow activity. A small load check:
run the smoke script from a few terminals at once and watch memory, CPU, `df` and the broker's response time; abort
if the broker slows noticeably. Then, in a browser on the dev origin: open a bill page with the chat,
ask a question, and confirm the answer links to our own `/explore/...` page, the "who supports" question lists
organizations from the broker, and the console shows no CORS error. In the journal, check there is **no**
"Slack service started" line and that the request was logged by this copy.

## 7. Cutover and rollback

- **Cutover** (SYNC-92 owns the criteria and the soak): point the new site's `wsUrl` (and any `?ddp_url=` links)
  at `<votebot-host>`, add the production origin (4.1), purge the widget cache. The Webflow site keeps using the
  old copy meanwhile.
- **Rehearse first, on the dev site**: switch it to the new copy and back, timing each way, including how long
  ddp-next takes to deploy the change and a tab reload, and test the old copy right before (its chat, its index,
  Slack handoff, the Webflow page). Do not cut over until the way back has been shown to work.
- **Go/no-go**: go only if section 6 passed, the baseline health table is unchanged after every step, and the
  rehearsal worked. Roll back at once if after cutover chat errors repeat, the broker slows, memory or disk
  pressure appears, an answer comes from the wrong index, or Slack/Webflow activity shows up on the new copy.
- **Rollback**: point the new site's `wsUrl` back to the old copy and purge the cache. Nothing on the old server
  was changed, so there is nothing to restore. To remove the new copy entirely: `sudo systemctl disable --now votebot`
  and delete its nginx server block (`nginx -t`, reload).

## 8. Checklist for the ticket

- [ ] 1.1 to 1.3 filled in, differences reported
- [ ] Section 2: `legacy-webflow` branch and tag in votebot and ddp-sync, old checkouts pinned, paths recorded
- [ ] Section 3: new copy running (`bill_filter_key=ocd_bill_id` in the startup log, no Slack), own Redis (port 6380, not the broker's), memory and log limits in place, real client IP restored behind Cloudflare
- [ ] Baseline health table filled in and unchanged after each step
- [ ] 4.1 production origin supplied and added at cutover; 4.2 widget path documented in `chat-widget/README.md`
- [ ] Section 5: weekly monitoring owner named; OpenAI usage limit and alert set; Cloudflare bot filtering tried only if needed
- [ ] Section 6 verified with real bills, failure paths and the small load check done; rollback rehearsed on the dev site
- [ ] Hand back to NEXT-36 (which `wsUrl`) and SYNC-92
