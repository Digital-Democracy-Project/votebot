# Phase 4: Remove Sync Code from VoteBot — COMPLETE

*Part of: DDP-Sync Migration (see PLAN-move-sync-to-own-service.md for overview)*
*Depends on: Phase 1 running and verified on EC2 (ddp-sync handles all sync jobs)*
*Completed: 2026-03-10*
*Commit: df25db5 — pushed to `main`*

## Goal

Strip all sync/ingestion/scheduler code from VoteBot, leaving it as a pure chat/RAG service. VoteBot's `main.py` drops from ~355 lines to ~131 lines. All sync routes are removed.

---

## Results

**37 files changed, +8 / -14,571 lines** (git reports -14,196 due to rename detection on `federal_legislator_cache.py`)

### Moved
| From | To |
|------|------|
| `src/votebot/sync/federal_legislator_cache.py` | `src/votebot/utils/federal_legislator_cache.py` |

### Modified
| File | Change |
|------|--------|
| `src/votebot/main.py` | 356→131 lines. Removed scheduler, leader election, watchdog, zombie sync resume. Kept Redis connect for chat |
| `src/votebot/api/routes/__init__.py` | Removed `sync_router`, `sync_unified_router` imports and exports |
| `src/votebot/core/retrieval.py` | Updated import: `votebot.sync.federal_legislator_cache` → `votebot.utils.federal_legislator_cache` |
| `src/votebot/config.py` | Removed `scheduler_enabled` field (3 lines) |
| `pyproject.toml` | Removed 4 sync-only deps: `tiktoken`, `PyPDF2`, `pdfplumber`, `apscheduler` |

### Deleted (~14,500 lines)
| Path | Lines |
|------|-------|
| `src/votebot/sync/` (entire dir) | ~3,557 |
| `src/votebot/updates/` (entire dir) | ~5,042 |
| `src/votebot/ingestion/` (entire dir) | ~4,553 |
| `src/votebot/api/routes/sync_unified.py` | 542 |
| `src/votebot/api/routes/sync.py` | 460 |
| `src/votebot/api/schemas/sync.py` | 51 |
| `config/sync_schedule.yaml` | 123 |

---

## Discovery: `embeddings.py` must be KEPT

The original plan called for deleting `src/votebot/services/embeddings.py` (181 lines). However, during verification it was found that **`vector_store.py` imports `EmbeddingService` for `embed_query()`** — this is used at chat time to embed user queries before searching Pinecone. The file was restored and is NOT deleted.

This means `embeddings.py` is shared between sync (embedding documents during ingestion) and chat (embedding queries during retrieval). Both VoteBot and ddp-sync need their own copy.

---

## Verification

### Import verification (all passed)
```
✓ No imports from votebot.sync (except utils.federal_legislator_cache)
✓ No imports from votebot.updates
✓ No imports from votebot.ingestion
✓ No imports from votebot.api.routes.sync
✓ No imports from votebot.api.schemas.sync
✓ embeddings.py exists and is imported by vector_store.py
✓ federal_legislator_cache.py exists in utils/ and is imported by retrieval.py
```

### Dependencies kept (used by chat)
- `pinecone` — retrieval queries
- `openai` — LLM generation + query embeddings
- `httpx` — HTTP calls to OpenStates for vote lookups
- `redis` — thread mapping, pub/sub
- `beautifulsoup4` — content resolution
- `tenacity` — retry logic in llm.py, web_search.py

### Dependencies removed (sync-only)
- `tiktoken` — token counting for chunking
- `PyPDF2` — PDF metadata extraction
- `pdfplumber` — PDF text extraction
- `apscheduler` — scheduled job execution

---

## Remaining verification (on EC2 deploy, Phase 7)

- [x] VoteBot starts without errors: `uvicorn votebot.main:app --port 8000` — confirmed running on EC2, see PLAN-ddp-sync-phase7.md
- [x] `GET /votebot/v1/health` returns healthy — confirmed on EC2, see PLAN-ddp-sync-phase7.md
- [ ] `GET /votebot/v1/health/ready` returns all services connected
- [x] Chat queries work: `POST /votebot/v1/chat` with a bill question — confirmed ("Chat queries responsive from DDP website"), see PLAN-ddp-sync-phase7.md
- [x] WebSocket chat works — confirmed ("WebSocket sessions reconnected after VoteBot restart"), see PLAN-ddp-sync-phase7.md
- [ ] Content resolution works: `GET /votebot/v1/content/resolve?url=...`
- [ ] No import errors in logs
- [ ] Sync endpoints return 404 (not 500): `POST /votebot/v1/sync/unified` → 404
- [ ] Federal legislator cache still works (used by retrieval.py for vote lookups)

The unchecked items above were not explicitly called out in Phase 7's deployment record and remain unverified (though nothing in the codebase or later commit history suggests a problem).
