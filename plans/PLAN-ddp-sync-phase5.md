# Phase 5: Remove Scheduler from DDP-API — COMPLETE

*Part of: DDP-Sync Migration (see PLAN-move-sync-to-own-service.md for overview)*
*Depends on: Phase 2 complete (ddp-sync runs all 11 scheduled jobs)*
*Completed: 2026-03-10*
*Commit: 94175e4 — pushed to DDP-API `main`*

## Goal

Remove `scheduler.py` (1,202 lines) from DDP-API. After this phase, DDP-API has zero background jobs — it's purely an auth gateway + API proxy.

---

## Results

**6 files changed, +11 / -1,666 lines**

### Deleted from ddp-api
| File | Lines | Reason |
|------|-------|--------|
| `scheduler.py` | 1,202 | All jobs moved to ddp-sync |
| `app/routes/sync.py` | 37 | Trigger endpoints (`/trigger_sync`, `/trigger_full_sync`) — replaced by catch-all proxy (Phase 6) |
| `tests/test_phone_conflict.py` | 386 | Tests `scheduler.py` functions directly — these tests now belong in ddp-sync |

### Modified in ddp-api
| File | Change |
|------|--------|
| `app/main.py` | 103 → 79 lines. Removed scheduler start/stop from lifespan, removed `sync_router` import/registration. Updated docstring to reflect proxy-only role |
| `app/routes/__init__.py` | Removed `sync_router` import and export |
| `requirements.txt` | Removed `APScheduler>=3.10.4` and `email-validator>=2.0.0`. Down from 10 to 8 dependencies |

### Not changed
- `middleware.py` — Legacy Flask app (not imported by FastAPI). Contains dead scheduler references but is not used
- `app/routes/votebot.py` — Still has 5 sync proxy handlers (`/votebot/sync/*`) that forward to VoteBot. These will be replaced by the catch-all proxy to ddp-sync in Phase 6

---

## Verification

### Import verification
```
✓ App imports cleanly — 30 routes registered
✓ No scheduler references in active code (only in unused middleware.py)
✓ email-validator only used by scheduler.py — safe to remove
✓ requests still used by voatz.py, brevo.py, middleware.py — kept
```

### Route verification (post-change)
```
Kept:
  /get_tokens, /get_users, /get_events, /create_event    (Voatz proxy)
  /update_segment_attribute, /user_updates                (Brevo proxy)
  /votebot/chat, /votebot/chat/stream, /votebot/feedback  (VoteBot chat proxy)
  /votebot/sync/* (5 endpoints)                           (VoteBot sync proxy — Phase 6 replaces)
  /votebot/ws                                             (WebSocket proxy)
  /webflow/* (8 endpoints)                                (Webflow CMS management)
  /, /health                                              (Health checks)

Removed:
  /trigger_sync                                           (was: calls scheduler.run_sync_job)
  /trigger_full_sync                                      (was: calls scheduler.run_full_sync_job)
```

### Runtime verification (on EC2 deploy, Phase 7)
- [ ] DDP-API starts without errors: `uvicorn app.main:app --port 5000`
- [ ] Logs show "DDP-API started (proxy mode)" — no scheduler messages
- [ ] `/health` endpoint returns healthy
- [ ] VoteBot chat proxy works: `POST /votebot/chat`
- [ ] WebSocket proxy works: `WS /votebot/ws`
- [ ] Voatz proxy works: `POST /get_tokens`, `/get_users`, `/get_events`
- [ ] Brevo proxy works: `POST /update_segment_attribute`, `/user_updates`
- [ ] Webflow CMS endpoints work: `POST /webflow/fill/session-code`, etc.
- [ ] Old trigger endpoints return 404: `POST /trigger_sync` → 404

---

## Ordering note

Phases 1-4 are already complete. Phase 5 only touches DDP-API.

**Important timing:** Between deploying Phase 5 and Phase 6 (catch-all proxy), the `/trigger_sync`, `/trigger_full_sync`, and `/votebot/sync/*` endpoints will return 404 or proxy errors. Deploy Phase 6 simultaneously with Phase 5 during the Wave 1 EC2 deployment.
