# Runbook: exposing VoteBot to the new website (ddp-next) on the civic host

Ticket: VOTEBOT-14. Blocks NEXT-36 (chat on the new site), and SYNC-92 for the second-instance part.
Run it through the prod agent (`ddp-broker-py` `notes/ops-handoff`). **Nothing in this document has been run.**
It was written from the repos only: the live nginx config and the real `.env` were not available, so
every change starts with a read-only check of what is deployed today. Fill the "Found" column as you go.

Two decisions are pending with Ramon and are written up with a recommendation, not settled:
[a second instance](#3-second-instance-on-ddp-knowledge-base-decision-pending-ramon) and
[rate limiting](#4-rate-limiting-decision-pending-ramon).

## 0. Order of work and ground rules

1. Section 1 (read-only checks). Stop and report if anything differs from what section 2 assumes.
2. Section 2 (changes to the existing instance), one at a time, each followed by its check.
3. Sections 3 and 4 **only after Ramon's decision line in that section is filled in**. Until then they are
   "do not execute".
4. Run `scripts/smoke_ws.py` (README, "WebSocket Smoke Test") against each endpoint you changed.

Ground rules for every change on the host:

- **Back up first, with a timestamp**, and keep the backup name in the ticket:
  `sudo cp -a /etc/nginx/sites-enabled/<file> /etc/nginx/<file>.bak-$(date +%Y%m%d-%H%M)`, and
  `cp -a ~/votebot/.env ~/votebot/.env.bak-$(date +%Y%m%d-%H%M)`.
- **`sudo nginx -t` before every `sudo systemctl reload nginx`**, and after the reload re-check that the existing
  production routes of 1.1 still answer from :8000. A reload with a bad config keeps the old one, but a good
  config with a wrong `location` does not announce itself.
- **Abort and roll back** (restore the backups, `nginx -t`, reload/restart, re-run 1.1's curls) if: a previously
  working route in 1.1 changes status, `journalctl -u votebot` shows errors after a restart that were not there
  before, the service does not come back within a minute, or free memory/swap drops as in 3.1.
- The prod agent (or whoever runs this) may abort at any point; report what was and was not changed.

## 1. Read-only checks

### 1.1 What nginx routes to `:8000` on `api.digitaldemocracyproject.org`

```bash
sudo nginx -T 2>/dev/null | grep -n -B3 -A14 -E 'ws/chat|votebot|widget'
```

Expected from the repo (`chat-widget/README.md`, "Step 3"): `location /votebot/` and `location /ws/chat`
proxied to `127.0.0.1:8000`, the second with `proxy_http_version 1.1`, `Upgrade`/`Connection: upgrade`
headers and `proxy_read_timeout 86400`. **Not documented anywhere in the repo**: whether
`/widget/ddp-chat.min.js` is a `location` with an `alias`/`root` (see 1.2). The VoteBot README says the
`/votebot` alias was removed from ddp-api (commit `95a648c`), so older "the widget talks through ddp-api"
descriptions may be stale; the `ddp-api` repo still has `app/routes/votebot.py`, a proxy, so check that
nothing is still routed through it.

Then from outside the box:

```bash
# Static widget: 200, and note the content-type, cache headers and (if present) CF-Cache-Status
curl -sI https://api.digitaldemocracyproject.org/widget/ddp-chat.min.js

# HTTP routes VoteBot owns: both public
curl -s https://api.digitaldemocracyproject.org/votebot/v1/features
curl -s "https://api.digitaldemocracyproject.org/votebot/v1/content/resolve?url=https://digitaldemocracyproject.org/bills/one-big-beautiful-bill-act-hr1-2025"

# WebSocket upgrade: expect "HTTP/1.1 101 Switching Protocols" (a 404/400/502 here means the route is wrong)
curl -i -N --max-time 5 \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
  -H "Sec-WebSocket-Key: $(openssl rand -base64 16)" \
  https://api.digitaldemocracyproject.org/ws/chat
```

| Check | Expected | Found |
|---|---|---|
| `/ws/chat` upgrades (101), long `proxy_read_timeout` |  |  |
| `/votebot/v1/content/resolve` reaches :8000 |  |  |
| `/votebot/v1/features` reaches :8000 |  |  |
| `/widget/ddp-chat.min.js` served; from where |  |  |
| anything still proxied through ddp-api |  |  |

### 1.2 Where `ddp-chat.min.js` lives and how it gets there

```bash
sudo grep -rn 'ddp-chat\|/widget' /etc/nginx/ 2>/dev/null          # the location/alias that serves the URL
sudo find / -name 'ddp-chat.min.js' -not -path '*/node_modules/*' 2>/dev/null
cd ~/votebot && git log -1 --format='%h %cd' -- chat-widget/dist/ddp-chat.min.js
sha256sum chat-widget/dist/ddp-chat.min.js $(sudo find / -name 'ddp-chat.min.js' -not -path '*/node_modules/*' 2>/dev/null)
curl -s https://api.digitaldemocracyproject.org/widget/ddp-chat.min.js | sha256sum   # what Cloudflare serves
```

Known from the repo and past deploys: build in `chat-widget/`, commit `dist/` with `git add -f`, pull on the
host (no CI), copy to `/var/www/votebot/` (`chat-widget/README.md`), purge the Cloudflare cache for the file.
**Unknown**: how `/widget/ddp-chat.min.js` maps to a file. Record the answer in `chat-widget/README.md`
("How a change reaches production") when found; if the three hashes above differ, the host is serving an old
bundle and VOTEBOT-12 will not be visible on the new site until that is fixed.

| Check | Found |
|---|---|
| nginx location serving `/widget/` and the directory behind it |  |
| repo bundle, on-disk bundle and Cloudflare-served bundle hashes equal? |  |
| who copies the file, and how |  |

### 1.3 VoteBot's real configuration (names and non-secret values only)

```bash
cd ~/votebot
grep -E '^(PINECONE_INDEX_NAME|PINECONE_NAMESPACE|ALLOWED_ORIGINS|DDP_BROKER_API_ROOT|USE_DDP_OPENSTATES_REPLICA|DDP_OPENSTATES_API_ROOT|REDIS_URL|VOTEBOT_QUICK_ACTION_BUTTONS)=' .env
# secrets: only whether they are set, never the value
grep -c '^DDP_OPENSTATES_BEARER_TOKEN=.' .env; grep -c '^SLACK_APP_TOKEN=.' .env
systemctl cat votebot.service; systemctl status votebot.service --no-pager | head -15
```

| Setting | Required by | Found |
|---|---|---|
| `PINECONE_INDEX_NAME` | **exactly `votebot-large` or unset** before deploying VOTEBOT-8/10 code. After VOTEBOT-11 any other value keeps legacy retrieval and logs a startup warning, but fix it anyway. |  |
| `ALLOWED_ORIGINS` | if set, it **replaces** the code default, so it must list every origin (see 2.1) |  |
| `DDP_BROKER_API_ROOT` | `/content/resolve` on ddp-next URLs; unset means 503 for those only |  |
| `USE_DDP_OPENSTATES_REPLICA`, `DDP_OPENSTATES_API_ROOT`, bearer token set? | version-aware retrieval (canonical index only) |  |
| `REDIS_URL`; `SLACK_APP_TOKEN` set? | section 3 |  |
| workers / `ExecStart` / `User` / `WorkingDirectory` / `EnvironmentFile` in the unit | section 3 template |  |

After any restart, the startup log line `VoteBot started (chat-only mode)` shows `pinecone_index_name` and
`bill_filter_key` (VOTEBOT-11); production must say `webflow_id`.

```bash
journalctl -u votebot -n 80 --no-pager | grep -E 'VoteBot started|PINECONE_INDEX_NAME'
```

## 2. Changes to the existing (production) instance

### 2.1 Allowed origins

Default in `src/votebot/config.py`: `https://digitaldemocracyproject.org`, `https://votebot.digitaldemocracyproject.org`,
`https://digital-democracy-project.webflow.io`. Add `https://dev.digitaldemocracyproject.org` now and the
production hostname of the new site at cutover. WebSockets are not subject to browser CORS and VoteBot has no
origin check on `/ws/chat`; only `/content/resolve` and `/features` are.

`ALLOWED_ORIGINS` is a pydantic list, so in `.env` it is a JSON array, and setting it replaces the default:

```bash
ALLOWED_ORIGINS=["https://digitaldemocracyproject.org","https://votebot.digitaldemocracyproject.org","https://digital-democracy-project.webflow.io","https://dev.digitaldemocracyproject.org"]
```

(If the existing `.env` already sets it, start from its value, not from this one.) At cutover add the new site's
production origin (exact scheme and host, **to be supplied by NEXT-36 / Ramon; none is guessed here**) and keep the
Webflow origins listed until Webflow is retired, so old and new coexist. Restart, then check:

```bash
curl -si -H 'Origin: https://dev.digitaldemocracyproject.org' https://api.digitaldemocracyproject.org/votebot/v1/features | grep -i access-control
```

### 2.2 `DDP_BROKER_API_ROOT`

Set it to the base URL of ddp-broker-py (value to be confirmed on the host; there is no code default and none
is guessed here), no trailing slash needed. It must serve the public `GET /api/bills/{id}/scorecard/` and
`GET /api/bills/resolve/`. Check before relying on it:

```bash
# GET, public, no auth. Expect 200 {"bill_openstates_id": "<bare uuid>"}, or 404 {"detail": "Bill not found"}
curl -s -w '\n%{http_code}\n' "$DDP_BROKER_API_ROOT/api/bills/resolve/?jurisdiction=FL&session=2026&gov_id=HB%201"
curl -s -o /dev/null -w '%{http_code}\n' "$DDP_BROKER_API_ROOT/api/bills/<a real broker bill id>/scorecard/"   # 200
```

Then `GET /votebot/v1/content/resolve?url=https://dev.digitaldemocracyproject.org/explore/FL/2026/HB%201`
(needs VOTEBOT-13) returns `ocd_bill_id`. A bill that exists in OpenStates but not in the broker is a 404.

### 2.3 Widget bundle path

From 1.2: make sure the served bundle is the repo's, record the real path and copy procedure in
`chat-widget/README.md`, and purge the Cloudflare cache for `ddp-chat.min.js` after every copy.

### 2.4 Restart and verify

```bash
sudo systemctl restart votebot && sleep 3 && journalctl -u votebot -n 40 --no-pager
python scripts/smoke_ws.py --url wss://api.digitaldemocracyproject.org/ws/chat --index legacy --cases <legacy cases>
```

Rollback for any of 2.1 to 2.3: restore the previous `.env` line / file and restart; nothing here touches the index.

## 3. Second instance on `ddp-knowledge-base` (decision pending Ramon)

**Decision (record here, and do not execute this section until it is filled in):** ______ (Ramon), date ______.

**Recommendation: yes.** ddp-next and acceptance testing use the new index while the production instance stays
on `votebot-large` for Webflow, so the cutover (SYNC-92) is a switch of one nginx path or one `.env` line, with
the production instance as the rollback. "Untouched" means its code, config and index are not changed; it still
shares the host (CPU, memory, nginx, Redis server), which is what 3.1 guards.

### 3.1 Preflight (read-only) and worker count

```bash
free -m; swapon --show; df -h / ~/votebot; nproc; ps -o pid,rss,cmd -C uvicorn      # RSS per worker
```

Start the second instance with **one uvicorn worker** (`--workers 1`) even if production runs two: acceptance
traffic is small, and a second pair of workers on a small host is the likeliest way this change hurts
production. Abort if available memory after starting it is under about 20% or swap is growing. Raise the count
only at cutover if load needs it.

Design, using the existing code and config only (no code change):

| Item | Value | Why |
|---|---|---|
| Unit | `votebot-next.service`, a copy of `votebot.service` (from 1.3) with a different `EnvironmentFile`, port and `SyslogIdentifier` | same checkout, same `venv`; one `git pull` updates both |
| Port | `127.0.0.1:8002` | 8000 is VoteBot, 8001 is DDP-Sync |
| Env file | `~/votebot/.env.next` | the production `.env` stays as it is |
| `PINECONE_INDEX_NAME` | `ddp-knowledge-base` (exactly; VOTEBOT-11 treats only that name as canonical) | selects `ocd_bill_id` retrieval |
| `USE_DDP_OPENSTATES_REPLICA`, `DDP_OPENSTATES_API_ROOT`, `DDP_OPENSTATES_BEARER_TOKEN` | as on production, replica **on** | version-aware retrieval needs api-v3's version fields |
| `DDP_BROKER_API_ROOT` | as 2.2 | `/content/resolve` on ddp-next URLs |
| `REDIS_URL` | `redis://localhost:6379/1` (a different db number from production) | `votebot:threads` (handoff thread mapping) and the button cache are per-db; pub/sub channels are global, which is harmless here |
| `QUERY_LOG_DIR` | `logs/queries-next` | keeps the two instances' analytics apart |
| `SLACK_BOT_TOKEN=`, `SLACK_APP_TOKEN=` | **set to empty** on this instance for now (see the precedence note below: leaving them out would inherit production's) | both instances opening Socket Mode with one app token would split Slack events between them, so handoff replies could reach the wrong instance. Human handoff is therefore off on the acceptance instance; decide how handoff works at cutover (SYNC-92) |
| `VOTEBOT_QUICK_ACTION_BUTTONS` | as on production | VOTEBOT-15 keys the cache by `ocd_bill_id` on this index |

**Settings precedence (checked against `votebot.config.Settings`).** Values from the unit's `EnvironmentFile`
win, but VoteBot also reads a `.env` in its working directory as a fallback, so anything *missing* from
`.env.next` silently comes from production's `.env`. That is what you want for the shared keys (OpenAI, Pinecone,
Webflow), and a trap for anything that must differ: set those explicitly in `.env.next`, **an empty value counts as set**
(`SLACK_APP_TOKEN=` overrides production's token, an absent line does not). After starting, read the effective
values from the startup log, not from the file.

What VoteBot writes to outside itself, so what sharing credentials can affect: Slack (handoff threads) and its
own log files. Pinecone is read-only from VoteBot, `WEBFLOW_SCHEDULER_API_KEY` is not read by any VoteBot code path
(README), and Webflow is only read at query time. Redis: the only pub/sub channels are `votebot:agent_events`
(each worker delivers an event only for a session it owns, so a foreign instance's events are ignored) and
`votebot:cache:invalidate` (idempotent deletes of slug keys); neither is isolated by db number and neither needs to be.

Unit sketch (fill `<...>` from `systemctl cat votebot`; do not invent values):

```ini
[Unit]
Description=VoteBot (ddp-next, ddp-knowledge-base index)
After=network.target redis.service

[Service]
User=<same as votebot.service>
WorkingDirectory=<same as votebot.service>
EnvironmentFile=<home>/votebot/.env.next
Environment=PYTHONPATH=src
ExecStart=<ExecStart of votebot.service with --port 8002>
Restart=on-failure
SyslogIdentifier=votebot-next

[Install]
WantedBy=multi-user.target
```

nginx, added to the `api.digitaldemocracyproject.org` server block, ddp-next only (backup and `nginx -t` per
section 0). A path prefix needs no DNS or certificate work, and the widget derives its HTTP base from `wsUrl` by
cutting at `/ws` (`chat-widget/src/widget.js`, `resolveContextFromUrl`), so
`wsUrl: 'wss://api.digitaldemocracyproject.org/next/ws/chat'` makes `/content/resolve` and `/features` go to
`/next/votebot/v1/...`. Because `proxy_pass` has a URI part (`/`), nginx replaces the matched `/next/` with `/`:

| Public URL | Port 8002 receives |
|---|---|
| `wss://api.../next/ws/chat?session_id=x` | `/ws/chat?session_id=x` |
| `https://api.../next/votebot/v1/content/resolve?url=...` | `/votebot/v1/content/resolve?url=...` |
| `https://api.../next/votebot/v1/features` | `/votebot/v1/features` |
| anything not under `/next/` | unchanged: port 8000 |


```nginx
location /next/ {
    proxy_pass http://127.0.0.1:8002/;      # trailing slash strips /next
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 86400;
}
```

(The widget bundle is shared; only the page's `wsUrl` differs.) A separate hostname is the alternative if a
prefix proves awkward.

Verify, in order:

```bash
sudo systemctl enable --now votebot-next
journalctl -u votebot-next -n 40 --no-pager | grep -E 'VoteBot started'      # bill_filter_key=ocd_bill_id
ss -ltnp | grep -E ':800[0-2]'                                               # 8002 is votebot-next only, 8000 unchanged
# the effective environment of the running process (names and emptiness only, never print the tokens)
sudo tr '\0' '\n' < /proc/$(systemctl show -p MainPID --value votebot-next)/environ \
  | sed -E 's/^(SLACK_[A-Z_]+|[A-Z_]*(KEY|TOKEN))=.+/\1=<set>/' | grep -E '^(PINECONE_INDEX_NAME|REDIS_URL|QUERY_LOG_DIR|SLACK_[A-Z_]+|[A-Z_]*(KEY|TOKEN))='
curl -s http://127.0.0.1:8002/votebot/v1/health/ready
curl -s https://api.digitaldemocracyproject.org/next/votebot/v1/features
python scripts/smoke_ws.py --url wss://api.digitaldemocracyproject.org/next/ws/chat --cases <real cases> --retrieval
```

Prove which instance answered (a 200 alone does not): after a `/next/` request, `journalctl -u votebot-next -n 20`
shows it and `journalctl -u votebot -n 20` does not. Then one browser check: open a page on the dev origin whose
widget uses the `/next/` `wsUrl`, send a message, and confirm in the browser console/network tab that the
`/content/resolve` call passes CORS and the socket opens, and that `journalctl -u votebot-next` logged it. There
must be no Slack activity from this instance (its logs show no "Slack service started").

`--retrieval` reads the index from the machine running the script, so run it on this host with `.env.next`
loaded (it prints which index it used). **Prerequisite:** the index must contain embedded bills (SYNC-83,
SYNC-91); an empty `ddp-knowledge-base` answers nothing, and Pinecone creates a missing index on first use.

Rollback: `sudo systemctl disable --now votebot-next` and remove the `location /next/` block. The production
instance was never changed. Rollback of the production instance itself, once it is on the new index, is
`PINECONE_INDEX_NAME=votebot-large` and a restart.

Cutover itself (SYNC-92, not here): either point production's `.env` at `ddp-knowledge-base` and restart, or move
the new site's `wsUrl` to the production path and retire the second unit; either way the old index and the old
unit stay available until the soak ends. Success criteria and rollback triggers belong in SYNC-92's runbook.

## 4. Rate limiting (decision pending Ramon)

VoteBot's WebSocket has no limit of its own (no auth, no per-session or per-IP cap; session ids are chosen by
the caller) and the new site is public. Every message costs OpenAI and Pinecone calls.

**Decision (record here, and do not execute this section until it is filled in):** ______ (Ramon), date ______.

**Recommendation: yes, at nginx, on the WebSocket handshake only, now.** Two things are limited: how often a
client may open a socket (`limit_req` counts the upgrade request) and how many sockets it may hold open
(`limit_conn`). No code, no new service. It is scoped to the WebSocket locations only, **not** to `/votebot/`,
`/next/` as a whole or the widget file, so the widget's HTTP calls and static asset are never throttled:

```nginx
# http { } level
limit_req_zone  $binary_remote_addr zone=votebot_new:10m  rate=30r/m;   # new sockets per client
limit_conn_zone $binary_remote_addr zone=votebot_open:10m;
limit_req_status  429;
limit_conn_status 429;

# production: inside the existing `location /ws/chat { ... }`
limit_req  zone=votebot_new burst=10 nodelay;    # nodelay: over the burst is rejected at once, never queued
limit_conn votebot_open 10;

# second instance: its own exact location for the socket (listed before `location /next/`)
location = /next/ws/chat {
    proxy_pass http://127.0.0.1:8002/ws/chat;
    # ...the same proxy_http_version / Upgrade / Connection / Host / X-Forwarded-* / proxy_read_timeout lines as /next/
    limit_req  zone=votebot_new burst=10 nodelay;
    limit_conn votebot_open 10;
}
```

Caveats to settle first:

- **Client IP.** If the API sits behind Cloudflare, `$binary_remote_addr` is Cloudflare's address unless nginx
  uses `real_ip` (`set_real_ip_from <each current Cloudflare range, IPv4 and IPv6, from cloudflare.com/ips>;
  real_ip_header CF-Connecting-IP;`). Check `nginx -T` for it; without it the limit applies to everyone behind an
  edge at once. Trust only those ranges, or a client can spoof the header and pick its own bucket. This is the
  first thing to verify.
- **Check after enabling:** open sockets normally from one browser (fine), then from a shell open 12 handshakes in
  a minute (the extra ones get 429), and from a second network a normal connection still works. To disable:
  remove the four lines, `nginx -t`, reload; VoteBot is not restarted.
- **Messages on an open socket are not limited by this** (the limits above act on the handshake). One connection can send many messages. Capping those
  needs an application-level limit (a per-session or per-IP counter in `handle_user_message`, Redis-backed
  because of the two workers). That is real code, so it is deliberately **not** proposed here; open a ticket if
  logs (`QueryLogger`, per-visitor counts) show it is needed.
- Shared networks (a school, a newsroom) share an IP; the numbers above are generous on purpose.

**Decision (record here):** ______ (Ramon), date ______.

## 5. Checklist for the ticket

- [ ] 1.1 to 1.3 filled in, differences reported
- [ ] 2.1 origins, 2.2 broker root, 2.3 widget path documented in `chat-widget/README.md`, 2.4 restart verified
- [ ] Decision recorded: second instance (section 3)
- [ ] Decision recorded: rate limiting (section 4), and the Cloudflare client-IP check done if yes
- [ ] If approved: `votebot-next` up, nginx `/next/`, smoke test passing against it
- [ ] Hand back to NEXT-36 (which `wsUrl` the new site uses) and SYNC-92 (second instance ready)
