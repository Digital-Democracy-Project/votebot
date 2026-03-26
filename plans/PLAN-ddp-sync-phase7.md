# Phase 7: Deployment — COMPLETE

*Part of: DDP-Sync Migration (see PLAN-move-sync-to-own-service.md for overview)*
*Depends on: Phase 6 code complete and tested locally*
*Completed: 2026-03-11*
*Note: Phases 1-6 code changes were all complete before deployment. Single-wave deployment.*

## Goal

Deploy the three-service architecture to the production EC2 instance. At the end of this phase, all three services are running independently under systemd, ddp-sync handles all sync jobs, and the old sync code is removed from VoteBot and DDP-API.

---

## Results

### Services deployed

| Service | Port | Status | Key Detail |
|---------|------|--------|------------|
| ddp-sync | 8001 | Running | 12 scheduled jobs, Python 3.11, systemd enabled |
| DDP-API | 5000 | Running | Proxy mode, catch-all proxy active, 29 routes |
| VoteBot | 8000 | Running | Chat-only, 2 uvicorn workers, no scheduler |

### EC2 instance

Stayed on **t3a.medium** (4GB RAM) — monitoring CloudWatch for memory pressure before upgrading to t3a.large.

Memory at deployment:
- VoteBot: ~144MB
- ddp-sync: ~123MB
- DDP-API: ~42MB
- Total: ~309MB (well within 4GB)

### Deployment timeline (2026-03-11 UTC)

1. 02:15 — Python 3.11 installed on EC2 (`deadsnakes/ppa`)
2. 02:19 — ddp-sync cloned, venv created, `pip install -e .` completed
3. 02:19 — ddp-sync systemd service started (12 jobs scheduled, health check OK)
4. 02:21 — DDP-API pulled (Phase 5+6), restarted (proxy mode)
5. 02:30 — VoteBot pulled (Phase 4), restarted (chat-only, WebSocket sessions reconnected)
6. 02:35 — DDP-API restarted again (Content-Type fix, commit 69279dd)
7. 02:39 — External sync test successful (single bill sync, 34 chunks, 16s)
8. 02:47 — Trigger routing debugged — nginx HTTPS config needed `/votebot/trigger` location
9. 02:50 — Redis key migration (461 bill version keys + 8 active jurisdictions)

### Fixes applied during deployment

1. **Content-Type header not forwarded** (commit `69279dd`): The catch-all proxy sent raw body bytes without `Content-Type`, causing ddp-sync validation errors. Fixed by forwarding `request.headers.get("content-type")`.

2. **nginx HTTPS config missing trigger route**: The SSL server block (`/etc/nginx/sites-enabled/api.digitaldemocracyproject.org`) had explicit `location /votebot/sync/` → DDP-API but `location /votebot/` → VoteBot directly, which caught `/votebot/trigger/*` and sent it to VoteBot (404). Added `location /votebot/trigger` pointing to DDP-API. Removed trailing slashes from both sync and trigger locations.

3. **Python 3.11 required**: EC2 had Python 3.10, ddp-sync requires `>=3.11`. Installed Python 3.11 from deadsnakes PPA. VoteBot and DDP-API venvs unaffected (isolated).

### Known issues discovered

1. **Redis health check error**: `/ddp-sync/v1/health` returns `"redis": "error: 'RedisStore' object has no attribute '_redis'"`. Cosmetic only — Redis works for actual operations. See TROUBLESHOOTING.md.

2. **Trigger endpoints are synchronous**: `/trigger/user-sync` and `/trigger/full-sync` block until completion (minutes). Should return task_id and run async. Noted in Phase 8 backlog.

---

## Pre-deployment: Secrets Manager

### ddp-sync/credentials — DONE (pre-existing)
`webflow_votebot_api_key` was already added (discovered during Phase 3).

### ddp-api/org-credentials — DONE
Added `ddp_sync_api_key` (same value as ddp-sync's `api_key`). No `ddp_sync_service_url` needed — hardcoded to `http://localhost:8001` in the proxy code.

---

## Verification results

### Health checks
```
✓ ddp-sync  — http://localhost:8001/ddp-sync/v1/health (healthy, 12 jobs, pinecone connected)
✓ DDP-API   — http://localhost:5000/health (healthy)
✓ VoteBot   — http://localhost:8000/votebot/v1/health (healthy, WebSocket sessions active)
```

### Config loading (ddp-sync)
```
Config source: secrets_manager
Pinecone index: votebot-large
Redis URL: redis://localhost:6379/0
API key set: True
Webflow scheduler key set: True
Webflow votebot key set: True
Organizations: 9
```

### Sync proxy (DDP-API → ddp-sync)
```
✓ POST /votebot/sync/unified — single bill sync, 34 chunks, 16s
✓ GET /votebot/sync/unified/status/test123 — returns "Task not found" (correct)
✓ POST /votebot/trigger/user-sync — Voatz→Brevo sync completed
```

### Chat (DDP-API → VoteBot)
```
✓ Chat queries responsive from DDP website
✓ WebSocket sessions reconnected after VoteBot restart
```

### Redis migration
```
✓ 461 bill_version keys copied (votebot:bill_version:* → ddp:bill_version:*)
✓ 8 active jurisdictions copied (votebot:active_jurisdictions → ddp:active_jurisdictions)
✓ Old keys expire naturally (90-day TTL)
```

### Pending verification
- [ ] Bill version sync runs from ddp-sync at 04:00 UTC (check next morning)
- [ ] Voatz→Brevo sync runs from ddp-sync every 30 minutes (check logs)
- [ ] Webflow batch jobs run from ddp-sync on Mondays at 03:00 UTC
- [ ] VoteBot logs show zero sync/scheduler activity

---

## nginx config changes

**File:** `/etc/nginx/sites-enabled/api.digitaldemocracyproject.org`

Added `/votebot/trigger` location, removed trailing slashes from sync/trigger locations:

```nginx
    # VoteBot sync endpoints (proxy through DDP-API for auth)
    location /votebot/sync {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120;
    }

    # VoteBot trigger endpoints (proxy through DDP-API to ddp-sync)
    location /votebot/trigger {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300;
    }
```

---

## Rollback procedures

### Rollback to VoteBot scheduler

```bash
# Revert VoteBot to pre-Phase-4 commit (restores sync/scheduler code)
cd /home/ubuntu/votebot
git checkout a48cfcf  # commit before Phase 4 (df25db5)
# Add SCHEDULER_ENABLED=true to .env
sudo systemctl restart votebot

# Revert DDP-API to pre-Phase-5 commit (restores scheduler.py)
cd /home/ubuntu/DDP-API
git checkout 60ef532  # commit before Phase 5 (94175e4)
sudo systemctl restart ddp-api

# Stop ddp-sync
sudo systemctl stop ddp-sync
sudo systemctl disable ddp-sync

# Revert nginx — remove /votebot/trigger location, restore trailing slash on /votebot/sync/
sudo nano /etc/nginx/sites-enabled/api.digitaldemocracyproject.org
sudo nginx -t && sudo systemctl reload nginx
```

---

## Post-deployment checklist

- [x] ddp-sync running on port 8001, 12 jobs scheduled
- [x] VoteBot running on port 8000, no scheduler (chat-only mode)
- [x] DDP-API running on port 5000, no scheduler, catch-all proxy active
- [x] Sync proxy works end-to-end (DDP-API → ddp-sync → Pinecone)
- [x] Trigger proxy works end-to-end (DDP-API → ddp-sync → Voatz/Brevo)
- [x] Chat queries work end-to-end (DDP-API → VoteBot → response)
- [x] WebSocket sessions reconnected after deployment
- [x] Redis key migration complete (ddp:* keys populated)
- [x] nginx HTTPS config updated for trigger routing
- [x] Secrets Manager updated (ddp_sync_api_key in ddp-api/org-credentials)
- [ ] EC2 upgraded to t3a.large — deferred, monitoring memory first
- [ ] Bill version sync verified from ddp-sync logs (04:00 UTC)
- [ ] Voatz→Brevo sync verified from ddp-sync logs (every 30 min)
- [ ] Webflow batch jobs verified from ddp-sync logs (Mon 03:00 UTC)
- [ ] CloudWatch alarms reviewed for new architecture
