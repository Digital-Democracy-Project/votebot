# Phase 6: Update DDP-API Proxy Routing — COMPLETE

*Part of: DDP-Sync Migration (see PLAN-move-sync-to-own-service.md for overview)*
*Depends on: Phase 1 running (ddp-sync is live on :8001)*
*Completed: 2026-03-10*
*Not yet committed — will be pushed with Phase 7 deployment*

## Goal

Add a catch-all proxy in DDP-API that forwards `/sync/*` and `/trigger/*` requests to ddp-sync. Remove the old per-endpoint VoteBot sync handlers. After this phase, DDP-API routes sync traffic to ddp-sync automatically — new ddp-sync endpoints are accessible without DDP-API changes.

---

## Results

**3 files changed: 1 new, 2 modified**

### New files (ddp-api)
| File | Lines | Purpose |
|------|-------|---------|
| `app/routes/ddp_sync_proxy.py` | 93 | Catch-all proxy forwarding `/sync/*` and `/trigger/*` to ddp-sync :8001 |

### Modified files (ddp-api)
| File | Change |
|------|--------|
| `app/main.py` | 79 → 87 lines. Import `ddp_sync_router`, register twice (with `/votebot` prefix and at root). +8 lines |
| `app/routes/votebot.py` | 429 → 236 lines. Deleted 5 sync handlers (193 lines) |

### Not changed
- `app/routes/__init__.py` — `ddp_sync_proxy` router imported directly in `main.py`, not via routes package
- `app/routes/sync.py` — Already deleted in Phase 5

### Config changes (TODO — deploy time)
| Source | Change |
|--------|--------|
| AWS `ddp-api/org-credentials` | Add `ddp_sync_service_url`, `ddp_sync_api_key` |
| DDP-API `.env` | Add `DDP_SYNC_SERVICE_URL`, `DDP_SYNC_API_KEY` (fallback) |

---

## Implementation details

### Catch-all proxy (`app/routes/ddp_sync_proxy.py`)

Two `api_route` handlers with path parameters:
- `@router.api_route("/sync/{path:path}")` — forwards to `/ddp-sync/v1/sync/{path}`
- `@router.api_route("/trigger/{path:path}")` — forwards to `/ddp-sync/v1/trigger/{path}`

Config loading follows same pattern as `votebot.py`: try `get_config()` (Secrets Manager), fall back to env vars.

Error handling:
- `httpx.ConnectError` → 502 "DDP-Sync service unavailable"
- `httpx.ReadTimeout` → 504 "DDP-Sync request timed out"
- `httpx.RequestError` → 502 with error message

Timeouts: 300s for POST (batch sync), 30s for GET.

### Router registration (`app/main.py`)

```python
# Catch-all proxy for ddp-sync — register under /votebot prefix
# so external paths don't change (e.g., /votebot/sync/unified → ddp-sync)
app.include_router(ddp_sync_router, prefix="/votebot")

# Also register trigger routes at root level (for /trigger/* paths)
app.include_router(ddp_sync_router)
```

Registered twice:
1. Under `/votebot` prefix — handles `/votebot/sync/*`, `/votebot/trigger/*` (existing external paths)
2. At root level — handles `/sync/*`, `/trigger/*` (new direct paths)

### Sync handlers removed from `votebot.py`

Deleted 5 handlers (193 lines):
- `POST /votebot/sync/bill` (old lines 177-212)
- `POST /votebot/sync/legislator` (old lines 215-250)
- `POST /votebot/sync/organization` (old lines 253-288)
- `POST /votebot/sync/unified` (old lines 291-331)
- `GET /votebot/sync/unified/status/{task_id}` (old lines 334-367)

Kept 4 handlers:
- `POST /votebot/chat` (lines 48-84)
- `POST /votebot/chat/stream` (lines 87-134)
- `POST /votebot/feedback` (lines 137-174)
- `WebSocket /votebot/ws` (lines 177-236)

---

## Verification

### Import verification
```
✓ App imports cleanly — 29 routes registered (was 30 before: 5 removed + 4 added)
✓ No sync handler references remain in votebot.py
✓ ddp_sync_proxy.py registered under /votebot prefix and at root level
```

### Route verification (post-change)
```
Kept (votebot.py → VoteBot :8000):
  /votebot/chat, /votebot/chat/stream, /votebot/feedback    (chat proxy)
  /votebot/ws                                                (WebSocket proxy)

New (ddp_sync_proxy.py → DDP-Sync :8001):
  /votebot/sync/{path:path}                                  (catch-all, /votebot prefix)
  /votebot/trigger/{path:path}                               (catch-all, /votebot prefix)
  /sync/{path:path}                                          (catch-all, root)
  /trigger/{path:path}                                       (catch-all, root)

Removed:
  /votebot/sync/bill                                         (was: proxy to VoteBot, returned 404)
  /votebot/sync/legislator                                   (was: proxy to VoteBot, returned 404)
  /votebot/sync/organization                                 (was: proxy to VoteBot, returned 404)
  /votebot/sync/unified                                      (was: proxy to VoteBot, returned 404)
  /votebot/sync/unified/status/{task_id}                     (was: proxy to VoteBot, returned 404)

Unchanged:
  /get_tokens, /get_users, /get_events, /create_event        (Voatz proxy)
  /update_segment_attribute, /user_updates                   (Brevo proxy)
  /webflow/* (8 endpoints)                                   (Webflow CMS management)
  /, /health                                                 (Health checks)
```

### Runtime verification (on EC2 deploy, Phase 7)
- [ ] `POST /votebot/sync/unified` routes to ddp-sync and returns task_id
- [ ] `GET /votebot/sync/unified/status/{id}` routes to ddp-sync and returns task status
- [ ] `POST /votebot/trigger/user-sync` triggers Voatz→Brevo sync via ddp-sync
- [ ] `POST /votebot/trigger/full-sync` triggers full-attribute sync via ddp-sync
- [ ] VoteBot chat endpoints still work (unaffected by changes)
- [ ] Webflow CMS endpoints still work (unaffected)
- [ ] Voatz/Brevo proxy endpoints still work (unaffected)
- [ ] Old single-item sync endpoints return 404 from ddp-sync (not 502/500)
- [ ] DDP-Sync health check accessible: `curl localhost:8001/ddp-sync/v1/health`
- [ ] Error case: if ddp-sync is down, DDP-API returns 502 (not 500 or hang)

---

## Route mapping after this phase

```
External Path                            Backend         Internal Path                  Handler
─────────────────────────────────────────────────────────────────────────────────────────────────
POST /votebot/chat                       VoteBot :8000   /votebot/v1/chat               votebot.py
POST /votebot/chat/stream                VoteBot :8000   /votebot/v1/chat/stream        votebot.py
POST /votebot/feedback                   VoteBot :8000   /votebot/v1/chat/feedback      votebot.py
WS   /votebot/ws                         VoteBot :8000   ws://.../ws/chat               votebot.py
POST /votebot/sync/unified               DDP-Sync :8001  /ddp-sync/v1/sync/unified      catch-all proxy
GET  /votebot/sync/unified/status/{id}   DDP-Sync :8001  /ddp-sync/v1/sync/uni.../...   catch-all proxy
POST /votebot/trigger/user-sync          DDP-Sync :8001  /ddp-sync/v1/trigger/user-sync catch-all proxy
POST /votebot/trigger/full-sync          DDP-Sync :8001  /ddp-sync/v1/trigger/full-sync catch-all proxy
POST /votebot/trigger/webflow/{job}      DDP-Sync :8001  /ddp-sync/v1/trigger/webflow/* catch-all proxy
POST /trigger/user-sync                  DDP-Sync :8001  /ddp-sync/v1/trigger/user-sync catch-all proxy (root)
POST /trigger/full-sync                  DDP-Sync :8001  /ddp-sync/v1/trigger/full-sync catch-all proxy (root)
POST /sync/unified                       DDP-Sync :8001  /ddp-sync/v1/sync/unified      catch-all proxy (root)
POST /get_tokens                         Voatz API       —                              voatz.py
POST /get_users                          Voatz API       —                              voatz.py
POST /get_events                         Voatz API       —                              voatz.py
POST /create_event                       Voatz API       —                              voatz.py
POST /update_segment_attribute           Brevo API       —                              brevo.py
POST /user_updates                       Brevo API       —                              brevo.py
POST /webflow/fill/session-code          Webflow API     —                              webflow.py
...                                      ...             ...                            ...
```

**Key point:** External paths are unchanged. `/votebot/sync/unified` still works — it just routes to ddp-sync instead of VoteBot now.

**Legacy single-item sync endpoints:** `/votebot/sync/bill`, `/votebot/sync/legislator`, `/votebot/sync/organization` are removed. The catch-all proxy routes them to ddp-sync as `/ddp-sync/v1/sync/bill` etc., but ddp-sync doesn't have those endpoints (use `/sync/unified` with `"mode": "single"` instead). They will return 404 from ddp-sync, which is correct — these were legacy endpoints.
