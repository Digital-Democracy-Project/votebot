# Phase 8: Update Monitoring and Documentation — COMPLETE

*Part of: DDP-Sync Migration (see PLAN-move-sync-to-own-service.md for overview)*
*Depends on: Phase 7 complete (all three services deployed)*
*Completed: 2026-03-11*
*Note: Phases 1-8 are COMPLETE. Three-service architecture is live on EC2.*

## Goal

Update monitoring, logging, and documentation to reflect the three-service architecture. Ensure ops tooling covers ddp-sync, and all READMEs accurately describe the new architecture.

---

## Step 1: Add ddp-sync to nginx health checks

If nginx is configured for upstream health checking, add the ddp-sync backend.

**File: `/etc/nginx/sites-available/default` (or equivalent)**

No change needed for routing — ddp-sync is not exposed externally via nginx. It's only accessible internally on `:8001`. DDP-API proxies all external traffic.

However, add a comment documenting the internal topology:

```nginx
# Internal service topology:
#   nginx (:80/443) → DDP-API (:5000) → VoteBot (:8000) [chat]
#                                      → DDP-Sync (:8001) [sync/data pipelines]
```

## Step 2: Add systemd watchdog for all three services

Create a simple monitoring script that checks all three services:

```bash
#!/bin/bash
# /home/ubuntu/scripts/check-services.sh
# Quick health check for all DDP services

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

check_service() {
    local name=$1
    local url=$2
    local response
    response=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$url")
    if [ "$response" = "200" ]; then
        echo -e "${GREEN}✓${NC} $name ($url) — HTTP $response"
    else
        echo -e "${RED}✗${NC} $name ($url) — HTTP $response"
    fi
}

echo "=== DDP Service Health ==="
check_service "DDP-API"   "http://localhost:5000/health"
check_service "VoteBot"   "http://localhost:8000/votebot/v1/health"
check_service "DDP-Sync"  "http://localhost:8001/ddp-sync/v1/health"

echo ""
echo "=== Systemd Status ==="
for svc in ddp-api votebot ddp-sync; do
    status=$(systemctl is-active $svc)
    if [ "$status" = "active" ]; then
        echo -e "${GREEN}✓${NC} $svc — $status"
    else
        echo -e "${RED}✗${NC} $svc — $status"
    fi
done

echo ""
echo "=== Scheduler ==="
curl -s http://localhost:8001/ddp-sync/v1/schedule | python3 -m json.tool 2>/dev/null || echo "Could not reach scheduler"
```

```bash
chmod +x /home/ubuntu/scripts/check-services.sh
```

## Step 3: Update CloudWatch alarms

The existing CloudWatch alarms were set for a single-service instance. Update for three services:

### Memory alarm

Already set at 75% of total RAM. After upgrading to t3a.large (8GB), 75% = 6GB. This is reasonable for 3 services. No change needed.

### Add per-service process monitoring (optional)

```bash
# Add to CloudWatch agent config to monitor individual services
# /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json

# Add procstat metrics for each service:
{
  "metrics": {
    "append_dimensions": {
      "InstanceId": "${aws:InstanceId}"
    },
    "metrics_collected": {
      "procstat": [
        {
          "pattern": "uvicorn ddp_sync.app:app",
          "measurement": ["cpu_usage", "memory_rss"],
          "metrics_collection_interval": 60
        },
        {
          "pattern": "uvicorn votebot.main:app",
          "measurement": ["cpu_usage", "memory_rss"],
          "metrics_collection_interval": 60
        },
        {
          "pattern": "uvicorn app.main:app",
          "measurement": ["cpu_usage", "memory_rss"],
          "metrics_collection_interval": 60
        }
      ]
    }
  }
}
```

This lets you see which service is consuming the most memory/CPU in CloudWatch dashboards.

## Step 4: Logging best practices

All three services now have cleanly separated logs:

```bash
# View logs by service
sudo journalctl -u ddp-sync -f          # Sync/scheduler activity only
sudo journalctl -u votebot -f           # Chat/RAG queries only
sudo journalctl -u ddp-api -f           # Proxy routing only

# Combined view (useful for tracing a request)
sudo journalctl -u ddp-api -u votebot -u ddp-sync -f

# Filter by time range
sudo journalctl -u ddp-sync --since "2026-03-10 04:00" --until "2026-03-10 05:00"

# Count errors in last 24 hours
sudo journalctl -u ddp-sync --since "24 hours ago" -p err --no-pager | wc -l
```

### Structured logging

ddp-sync uses `structlog` (already in dependencies). Ensure all pipeline modules use structured logging for easy parsing:

```python
# Example in ddp_sync/pipelines/bill_version.py
import structlog
logger = structlog.get_logger()

# Instead of:
logger.info(f"Synced bill {bill_id}: status={status}")

# Use:
logger.info("bill_synced", bill_id=bill_id, status=status, webflow_skipped=skipped)
```

This makes logs grep-friendly and CloudWatch Logs Insights queryable.

## Step 5: Documentation updates

### ddp-sync README.md

Create `ddp-sync/README.md`:

```markdown
# DDP-Sync

Unified data pipeline service for the Digital Democracy Project.

## Architecture

DDP-Sync handles all scheduled and on-demand data sync operations:

- **Bill version sync** (daily): OpenStates → Webflow CMS + Pinecone
- **Legislator sync** (weekly): OpenStates → Pinecone
- **Organization sync** (monthly): Webflow → Pinecone
- **Voatz → Brevo user sync** (every 30 min): Voatz → Brevo contact lists
- **Voatz → Brevo full-attribute sync** (monthly): Full re-import
- **Webflow CMS batch jobs** (weekly): Fill fields, sync refs, detect duplicates

## Service topology

```
DDP-API (:5000) — Auth gateway + API proxy
  ├── VoteBot (:8000) — Chat/RAG
  └── DDP-Sync (:8001) — Data pipelines (this service)
```

## Configuration

Production: AWS Secrets Manager (`ddp-sync/credentials`)
Local dev: `.env` file (copy from `.env.example`)

## API

| Method | Path | Description |
|--------|------|-------------|
| POST | /ddp-sync/v1/sync/unified | Trigger batch or single sync |
| GET | /ddp-sync/v1/sync/unified/status/{id} | Poll task status |
| POST | /ddp-sync/v1/sync/unified/all | Trigger sync for all content types |
| POST | /ddp-sync/v1/trigger/user-sync | Trigger Voatz→Brevo sync |
| POST | /ddp-sync/v1/trigger/full-sync | Trigger full-attribute sync |
| POST | /ddp-sync/v1/trigger/webflow/{job} | Trigger specific Webflow batch job |
| GET | /ddp-sync/v1/health | Health check |
| GET | /ddp-sync/v1/schedule | Show scheduled jobs |

## Deployment

```bash
cd /home/ubuntu/ddp-sync
git pull origin main
sudo systemctl restart ddp-sync
```

## Logs

```bash
sudo journalctl -u ddp-sync -f
```
```

### VoteBot README.md updates

Update the VoteBot README to reflect its new role:

```markdown
## Architecture

VoteBot is a **chat/RAG service** that provides AI-powered responses about
legislation, legislators, and organizations.

- HTTP chat: POST /votebot/v1/chat
- Streaming: POST /votebot/v1/chat/stream
- WebSocket: WS /ws/chat (with Slack handoff)
- Content resolution: GET /votebot/v1/content/resolve

**Sync/ingestion is handled by [ddp-sync](https://github.com/Digital-Democracy-Project/ddp-sync).**
VoteBot reads from Pinecone (populated by ddp-sync) and Webflow CMS (managed by ddp-sync).
```

Remove or update these sections (much of this is already gone from the codebase after Phase 4):
- Scheduler configuration — code removed, no `scheduler_enabled` setting
- Sync endpoints documentation — routes removed, return 404
- Ingestion pipeline documentation — code removed
- Leader election / zombie watchdog documentation — code removed

### VoteBot TROUBLESHOOTING.md updates

Update sync-related troubleshooting sections:

```markdown
## Sync issues

Sync is now handled by ddp-sync. Check ddp-sync logs:

```bash
sudo journalctl -u ddp-sync -n 100 --no-pager
```

To trigger a manual sync:
```bash
curl -X POST http://localhost:8001/ddp-sync/v1/sync/unified \
  -H "Authorization: Bearer $DDP_SYNC_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"type": "bill", "mode": "batch"}'
```
```

### DDP-API README.md updates

```markdown
## Architecture

DDP-API is an **auth gateway + API proxy** that routes requests to internal services.
87-line `main.py`, no scheduler, no background jobs.

### Services
- **VoteBot** (:8000) — Chat/RAG
- **DDP-Sync** (:8001) — Data pipelines

### Route mapping (29 routes)
- `/votebot/chat`, `/votebot/chat/stream`, `/votebot/feedback`, `/votebot/ws` → VoteBot :8000 (votebot.py)
- `/votebot/sync/*`, `/votebot/trigger/*` → DDP-Sync :8001 (catch-all proxy, ddp_sync_proxy.py)
- `/sync/*`, `/trigger/*` → DDP-Sync :8001 (catch-all proxy, root level)
- `/get_tokens`, `/get_users`, `/get_events`, `/create_event` → Voatz API (voatz.py)
- `/update_segment_attribute`, `/user_updates` → Brevo API (brevo.py)
- `/webflow/*` → Webflow CMS (webflow.py)

**Adding new ddp-sync endpoints requires NO DDP-API changes** — the catch-all proxy
forwards `/sync/*` and `/trigger/*` automatically.
```

Remove:
- Scheduler documentation
- `scheduler.py` references
- Sync interval configuration
- Per-endpoint sync handler documentation

---

## Step 6: Update VoteBot RAG_ARCHITECTURE.md

If this doc exists, update the "Data Ingestion" or "Sync Scheduling" sections to reference ddp-sync.

```bash
# Check if it exists
ls /home/ubuntu/votebot/docs/RAG_ARCHITECTURE.md
```

Key changes:
- "Bill sync runs daily at 04:00 UTC" → "Bill sync runs daily at 04:00 UTC via ddp-sync"
- Remove leader election documentation
- Remove in-process scheduler documentation
- Add "See ddp-sync for ingestion pipeline details"

---

## Verification checklist

- [x] `check-services.sh` script created (`ddp-sync/infrastructure/check-services.sh`)
- [ ] CloudWatch agent running, per-service process metrics visible (optional, deferred)
- [x] ddp-sync `README.md` created with API docs and deployment instructions
- [x] VoteBot README updated — scheduler/sync scheduling sections removed, points to DDP-Sync
- [x] DDP-API README updated — no scheduler references, proxy docs added, secrets example updated
- [x] VoteBot TROUBLESHOOTING.md updated — DDP-Sync issues section added (commit 5275ddb)
- [x] VoteBot RAG_ARCHITECTURE.md updated — sync scheduling section points to DDP-Sync
- [ ] All three `journalctl -u <service>` streams show only relevant logs (verified during Phase 7)

---

## Post-deployment improvements (backlog)

### Make trigger endpoints async (return task_id)

The `/trigger/user-sync` and `/trigger/full-sync` endpoints currently run synchronously — the HTTP request blocks until the entire Voatz→Brevo sync completes (can take several minutes). This causes nginx timeouts and a poor caller experience.

**Fix:** Run the sync in a background task and return a `task_id` immediately, matching the `/sync/unified` pattern. Callers can poll `/sync/unified/status/{task_id}` for progress.

**Files to change:**
- `ddp_sync/api/routes/triggers.py` — wrap `run_sync_job()`/`run_full_sync_job()` in a background task, return `{"task_id": ..., "status": "running"}`
- Reuse the existing task state infrastructure from `sync/unified`

### Fix Redis health check error — DONE (commit `8a15de9`)

Fixed: `_redis` → `_client` in health check and zombie watchdog. Also made Pinecone client initialization lazy so ddp-sync starts without `PINECONE_API_KEY`.

### Fix scheduler config path for non-editable install — DONE (commit `8a15de9`)

`scheduler.py` now resolves `sync_schedule.yaml` from both package-relative path and CWD fallback. Non-editable install (`pip install .`) works correctly when running from the repo root.

### Migrate repos to Digital-Democracy-Project org — DONE (2026-03-11)

All three repos migrated to the `Digital-Democracy-Project` GitHub org:
- `Digital-Democracy-Project/votebot` (was `VotingRightsBrigade/votebot`)
- `Digital-Democracy-Project/ddp-api` (was `VotingRightsBrigade/DDP-API`)
- `Digital-Democracy-Project/ddp-sync` (already created there)

EC2 remotes updated for all three services. DDP-API sanitized for public visibility (vendor-specific URLs externalized to env vars, example config IDs replaced with placeholders).

### Sanitize DDP-API for public repo — DONE (commit `0f754d6`, `b23e93b`)

- Externalized Voatz vendor URL to `VOATZ_API_BASE_URL` and `VOATZ_API_ORIGIN` env vars
- Replaced real org IDs and list IDs with placeholders in example configs
- Removed `.idea/` directory and updated `.gitignore`

---

## Files created/modified (this phase)

### New files
| File | Purpose |
|------|---------|
| `ddp-sync/README.md` | Service documentation (commit 4dcf127) |
| `ddp-sync/infrastructure/check-services.sh` | Health check script for all 3 services (commit 4dcf127) |

### Modified files
| File | Change | Commit |
|------|--------|--------|
| VoteBot `README.md` | Remove sync scheduling, update arch tree, point to DDP-Sync | e45017b |
| VoteBot `docs/TROUBLESHOOTING.md` | Add DDP-Sync issues section | 5275ddb |
| VoteBot `docs/RAG_ARCHITECTURE.md` | Update sync scheduling section | e45017b |
| DDP-API `README.md` | Remove scheduler docs, add proxy docs, update secrets example | d882aa8 |
