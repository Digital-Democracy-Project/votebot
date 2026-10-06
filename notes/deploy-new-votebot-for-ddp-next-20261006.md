# Deploy the new VoteBot for ddp-next: how to roll out the VOTEBOT-11 to -16 changes

**Date:** 2026-10-06
**Filed under:** VOTEBOT-14 (host work), VOTEBOT-15, VOTEBOT-16, and the code changes in VOTEBOT-11/12/13.
**From:** Ramon's planning agent (no access to either server, Pinecone, the broker or api-v3; **nothing below has been run anywhere**).
**Ask:** deploy the new VoteBot on the new-infrastructure server, verify it, and report back. **Do not switch the website to it and do not touch the old copy beyond the freeze in step 2.** Stop at every "STOP" and report instead of improvising.

The full reference is `docs/RUNBOOK-civic-host-ddp-next.md` on `main` (`git show origin/main:docs/RUNBOOK-civic-host-ddp-next.md`). This note is the short, ordered version of it plus what changed since. Where they differ, tell us.

## The layout (decided by Ramon, 2026-10-05)

| | Old VoteBot (Webflow) | New VoteBot (ddp-next) |
|---|---|---|
| Server | the existing civic server | the new-infrastructure server (ddp-broker-py, ddp-open-states, new ddp-sync) |
| Code | **frozen** on branch `legacy-webflow`, no updates | `main` |
| Index | `votebot-large` | `ddp-knowledge-base` |
| Slack handoff | stays here | **off** (one Slack connection per app token) |
| Webflow | still used | none |

## What changed (so you know what you are deploying)

- **Canonical index mode.** Exactly `PINECONE_INDEX_NAME=ddp-knowledge-base` switches VoteBot to `ocd_bill_id` retrieval. Any other name keeps the old `webflow_id` mode and logs a startup warning (`votebot-dev`, typos and so on no longer flip it).
- **Bills:** answers use the bill's current version by default (version labels come from api-v3, so `USE_DDP_OPENSTATES_REPLICA=true` is required); `/content/resolve` understands ddp-next `/explore/{JURISDICTION}/{SESSION}/{IDENTIFIER}` URLs through the broker.
- **Live, not embedded** (they are not in the index): **votes** (live OpenStates lookup), **legislators** (api-v3 `/people`), **"what changed"** (api-v3's stored diff; only the latest two versions have one), **organization positions and profiles** (broker public reads).
- **Links:** with `DDP_SITE_BASE_URL` set, bill sources and citations link to our own `/explore/...` pages.
- **Chat widget:** the bundle now passes `ocd_bill_id`, `url` and `session-code`, and detects bill changes by `ocd_bill_id`.
- **WebSocket:** the `stream_end` frame now carries `bill_votes_tool_used`.
- **Smoke test:** `scripts/smoke_ws.py` (README, "WebSocket Smoke Test").

## Prerequisites: check before you start

1. `git fetch origin && git log --oneline origin/main | head -20` must show the merges of **PR #17** (legislators and "what changed" live from api-v3) **and PR #19** (declares `aiofiles`, see below). If either is missing, **STOP and tell us**; deploy the SHA of `main` that contains both and record it.
2. The canonical index must **already hold embedded bills** (ddp-sync, SYNC-83/SYNC-91). Check read-only in step 3.4. If it is empty or missing: **STOP**. Pinecone *creates a missing index on first use*, so starting VoteBot against a wrong or misspelled index name would silently create an empty one.
3. A free local port for VoteBot (this note uses `8002`) and the broker's and api-v3's **local** URLs (step 1).

## Ground rules for every change on a host

- Back up before editing: `sudo cp -a <file> <file>.bak-$(date +%Y%m%d-%H%M)`; put the backup names in your reply.
- `sudo nginx -t` before every `sudo systemctl reload nginx` (reload, never restart), then re-run the baseline checks below.
- **Baseline health of what already runs on the new server** (broker, api-v3/ddp-open-states, ddp-sync): write down how you checked each and the result **before the first change**, and repeat after installing Redis, after starting VoteBot, after each nginx reload. Any change in their health: roll back that step and STOP.
- **Abort and roll back** (restore backups, reload/restart, repeat the checks) if a previously working route changes status, a restarted service does not come back within a minute, free memory or disk drops sharply, or an answer comes from the wrong index.
- Do **not**: run any sync or embedding job, rescrape anything, write to Pinecone, touch the `votebot-large` index or the old VoteBot's `.env`, set Slack tokens on the new copy, change any website's configuration, or run `redis-cli` against the broker's Redis (port 6379).

## Step 1: read-only checks (new server). Report the answers

```bash
free -m; swapon --show; df -h /; nproc
ss -ltnp | grep -E ':(80|443|8000|8001|8002|6379|6380)\b'
systemctl list-units --type=service --state=running | grep -E 'broker|openstates|sync|redis|nginx|votebot'
ps -eo pid,rss,cmd --sort=-rss | head -12
sudo nginx -T 2>/dev/null | grep -n server_name       # hostnames in use; is the VoteBot hostname free?
```

Find and record: the broker's **local** URL (this becomes `DDP_BROKER_API_ROOT`), the api-v3 URL and bearer token (`DDP_OPENSTATES_API_ROOT`, `DDP_OPENSTATES_BEARER_TOKEN`; the token value is **not** to be written into any note), whether Cloudflare is in front, and whether the box is idle (ddp-sync mostly starts Fargate jobs; scorecard creation is the one heavy job and is moving to Fargate): note memory **during** a scorecard run if you can.

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<broker port>/api/organizations/                   # 200
curl -s "http://127.0.0.1:<broker port>/api/bills/resolve/?jurisdiction=FL&session=2026&gov_id=HB%201"        # {"bill_openstates_id": "<uuid>"} or 404 {"detail": "Bill not found"}
```

## Step 2: freeze the old copy (old server; no behaviour change)

1. Record the **full commit SHA** and clean/dirty state of the old VoteBot checkout and of the old ddp-sync checkout (`git log -1`, `git status -sb`), and save copies of each unit file (`systemctl cat`), the path and mode of each `.env` (**not** the contents), the Python version and `pip freeze`, and the restart commands.
2. In **both** repos, from exactly those SHAs: create the branch `legacy-webflow` and the tag `legacy-webflow-2026-10`, and push them. (If you cannot push, give us the SHAs and we will.)
3. On the old server check each checkout out on `legacy-webflow` and leave it there. **From now on the old server never pulls `main`.** Do not restart the old services. Do not change its `.env`. Its `PINECONE_INDEX_NAME` must be exactly `votebot-large` (read it, do not edit).

## Step 3: build the new copy (new server)

### 3.1 User and code

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin votebot
sudo -u votebot git clone git@github.com:Digital-Democracy-Project/votebot.git /home/votebot/votebot
cd /home/votebot/votebot && sudo -u votebot git checkout <the main SHA from the prerequisites>
sudo -u votebot python3 -m venv venv && sudo -u votebot venv/bin/pip install -e .
sudo -u votebot venv/bin/python -c "import aiofiles, websockets, httpx; print('deps ok')"
```

Python 3.11 or newer. **Check the last line prints `deps ok`.** Before PR #19, `pip install -e .` did not install `aiofiles` and query logging was silently off; if the import fails, install `aiofiles` and tell us.

### 3.2 Its own Redis (a separate process, never the broker's)

`/etc/redis/votebot.conf` (adapt the unit name to how redis is run on this host):

```conf
bind 127.0.0.1
port 6380
save ""
appendonly no
maxmemory 256mb
maxmemory-policy allkeys-lru
```

Run it as its own systemd unit and check `redis-cli -p 6380 ping`. VoteBot only keeps a button cache and a handoff map in it, so no persistence is needed.

### 3.3 `/home/votebot/votebot/.env` (owner `votebot`, mode 600; a fresh file, nothing copied from the old server)

```bash
ENVIRONMENT=production
API_KEY=<new random key>
OPENAI_API_KEY=<a key from a dedicated OpenAI project for this deployment>
PINECONE_API_KEY=<...>
PINECONE_INDEX_NAME=ddp-knowledge-base          # exactly this
PINECONE_NAMESPACE=<the namespace the new ddp-sync writes; confirm, default is "default">
DDP_BROKER_API_ROOT=<broker local URL from step 1>
USE_DDP_OPENSTATES_REPLICA=true
DDP_OPENSTATES_API_ROOT=<api-v3 URL from step 1>
DDP_OPENSTATES_BEARER_TOKEN=<...>
DDP_SITE_BASE_URL=https://dev.digitaldemocracyproject.org     # dev host for now; production host is not decided; leaving it unset is a safe no-op
ALLOWED_ORIGINS=["https://dev.digitaldemocracyproject.org"]   # JSON array; REPLACES the code default; no Webflow origins here
REDIS_URL=redis://127.0.0.1:6380/0
QUERY_LOG_DIR=/home/votebot/votebot/logs/queries
VOTEBOT_QUICK_ACTION_BUTTONS=<same value as on the old server>
SLACK_BOT_TOKEN=
SLACK_APP_TOKEN=
```

No `WEBFLOW_*` variables at all. The two empty Slack lines are deliberate. `WEBFLOW_ORG_LOOKUP_ENABLED=false` is the switch that turns off the runtime organization-position lookups if the broker misbehaves (needs a restart).

### 3.4 Confirm the index exists and holds bills (read-only), **before** starting the service

```bash
sudo -u votebot bash -c 'cd /home/votebot/votebot && set -a && . ./.env && set +a && venv/bin/python -' <<'EOF'
import os
from pinecone import Pinecone
pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
names = [i.name for i in pc.list_indexes()]
print("indexes:", names)
assert "ddp-knowledge-base" in names, "STOP: the index does not exist; do not start VoteBot"
print(pc.Index("ddp-knowledge-base").describe_index_stats())   # look for your namespace with a non-zero vector count
EOF
```

If the index is missing, or the namespace you configured has no vectors: **STOP and report** (the data is not there yet; this is not a VoteBot fault).

### 3.5 Service

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

**One worker, on purpose** (chat history lives in the worker's memory). Then:

```bash
sudo mkdir -p /home/votebot/votebot/logs/queries && sudo chown -R votebot: /home/votebot/votebot/logs
sudo systemctl daemon-reload && sudo systemctl enable --now votebot
journalctl -u votebot -n 60 --no-pager | grep -E 'VoteBot started|PINECONE_INDEX_NAME|Error|Traceback'
curl -s http://127.0.0.1:8002/votebot/v1/health/ready
```

**The startup line must show `pinecone_index_name=ddp-knowledge-base` and `bill_filter_key=ocd_bill_id`, and there must be no "neither the legacy nor the canonical index" warning.** If it says `webflow_id`, the index name is wrong: stop the service and report. Repeat the baseline checks.

Logs: install `/etc/logrotate.d/votebot` for `/home/votebot/votebot/logs/queries/*.jsonl` (daily, rotate 14, compress, missingok, notifempty, `su votebot votebot`). The files hold visitors' messages and addresses.

### 3.6 nginx (new hostname; name to be chosen, `<votebot-host>`)

Backup, then add a server block proxying to `127.0.0.1:8002` (`/ws/chat` with `proxy_http_version 1.1`, `Upgrade`, `Connection "upgrade"`, `proxy_read_timeout 86400`; `/votebot/` as a normal proxy; both with `Host`, `X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto`). Check first that the name is not already used (`nginx -T | grep server_name`) and that the certificate covers it. **No connection limits** (Ramon's decision: monitor first). If Cloudflare is in front, add `set_real_ip_from` for each current Cloudflare range (IPv4 and IPv6) and `real_ip_header CF-Connecting-IP;` so logged addresses are real. `nginx -t`, reload, repeat the baseline checks.

## Step 4: verify (still without switching any website)

1. `curl -s https://<votebot-host>/votebot/v1/features` and, with the broker configured, `curl -s "https://<votebot-host>/votebot/v1/content/resolve?url=https://dev.digitaldemocracyproject.org/explore/FL/2026/HB%201"` should return JSON with an `ocd_bill_id` (a 404 means the broker does not know that bill: try another).
2. **The five-query smoke test, on the new server, with its `.env` loaded** (the cases pick an embedded bill per jurisdiction themselves):

   ```bash
   sudo -u votebot bash -c 'cd /home/votebot/votebot && set -a && . ./.env && set +a && \
     venv/bin/python scripts/smoke_ws.py --url wss://<votebot-host>/ws/chat --cases scripts/smoke_cases.json --retrieval'
   ```

   Paste the whole output into your reply. Expected: `5/5 cases passed` plus the "index holds no bill-votes or bill-version-diff" line passing. A line `no embedded bill-text found for XX`: that jurisdiction has no embedded bills yet (report which). `bill_votes_tool_used is False, expected True`: the live votes lookup is not working (check `USE_DDP_OPENSTATES_REPLICA`, the api-v3 URL and token). `positive control failed`: wrong index or namespace.
3. **Legislator and "what changed" checks** (not in the smoke cases). Save as `/tmp/extra-cases.json` (replace the bracketed values with a legislator and a bill you know exist) and run the same command with `--cases /tmp/extra-cases.json --min-confidence 0.4` and **without** `--retrieval` (answers that use only live data have no retrieved sources, so their confidence score is lower):

   ```json
   [
     {"name": "legislator live from api-v3",
      "page_context": {"type": "general"},
      "questions": [{"message": "Who is Senator <Full Name>, and what party are they in?", "expect_any": ["Republican", "Democrat", "Democratic", "Independent"]}]},
     {"name": "what changed is read live",
      "page_context": {"type": "bill", "id": "<gov id>", "jurisdiction": "<XX>", "session": "<session>", "ocd_bill_id": "<uuid of a bill with 2+ versions>"},
      "questions": [{"message": "What changed in the latest version of this bill?", "expect_regex": ["added|removed|changed|amend|struck|inserted"]}]}
   ]
   ```

   A reply saying it "could not retrieve" the live data means api-v3 is not answering: check the api-v3 URL and token, then report.
4. Failure paths, once: stop the broker briefly in a quiet moment (or point `DDP_BROKER_API_ROOT` at a closed port and restart) and confirm answers still come, just without organization positions; restart VoteBot during a chat and confirm the widget reconnects (history is lost: expected); confirm the journal shows **no** "Slack service started" and no Webflow requests. A small load check: run the smoke script from a few terminals at once while watching memory, CPU, `df` and the broker's response time; abort if the broker slows noticeably.
5. Baseline health table complete and unchanged after every step.

## Step 5: the widget bundle (report; do not publish yet)

The bundle is the same for both copies. Find where **ddp-next serves static files** and report the path and how a file gets there (this is undocumented today). Report `sha256sum chat-widget/dist/ddp-chat.min.js` from the deployed SHA. The new site must be configured with `wsUrl: 'wss://<votebot-host>/ws/chat'` (NEXT-36 owns that; do not edit ddp-next).

## What to report back

Reply as a new file `notes/deploy-new-votebot-for-ddp-next-reply-<date>.md` on this branch, with:

1. The deployed **SHA** of `main`, and the **SHAs** of the old VoteBot and old ddp-sync at the freeze (step 2), plus where the branch and tag were pushed.
2. The "found" values of steps 1 and 3 that are not secrets: ports, hostnames, the broker and api-v3 **URLs** (not tokens), memory and disk numbers, whether Cloudflare is in front, the Redis unit name, the nginx config file edited and its backup name.
3. The startup log line, the smoke-test output (pasted), the extra checks' results, the baseline health table.
4. Anything that surprised you or differed from this note or the runbook, and every STOP you hit.

## Not verified by us (please treat as open)

- Nothing here has run against a real api-v3, broker or the new index; the code is tested with mocked HTTP. The smoke test above is the first real check.
- `document_id` on the real vectors equals api-v3's current version (the `--retrieval` check asserts it; this has never been seen on real data).
- The production hostname of the new site, the new VoteBot's hostname, and the Pinecone namespace are placeholders to be read off the systems or decided by Ramon.
- Cutover, rollback rehearsal and the production origin are SYNC-92 / NEXT-36, not this task.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
