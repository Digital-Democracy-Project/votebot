# Phase 3: Simplify ddp-sync Internals

*Part of: DDP-Sync Migration (see PLAN-move-sync-to-own-service.md for overview)*
*Depends on: Phase 1 and Phase 2 complete*

## Goal

Remove complexity that was only needed because sync shared a process with VoteBot's chat workers. With ddp-sync as a single-worker standalone service, leader election is unnecessary and the zombie watchdog can be simplified.

---

## Status: COMPLETE

Implemented and verified on 2026-03-10. All 9 verification checks passed.

### Additional discovery: `.get_secret_value()` migration

VoteBot uses pydantic `SecretStr` for API keys (requiring `.get_secret_value()` to access the value). ddp-sync uses a plain `dataclass` with `str` fields. During Phase 1/2 copy, all `.get_secret_value()` calls were copied verbatim — they would have caused `AttributeError` at runtime on EC2.

**Fixed in this phase:** Removed `.get_secret_value()` from all 14 files across the codebase. Also added the missing `webflow_votebot_api_key` field to `SyncSettings` (was referenced by many files but never added to the config dataclass).

---

## Step 1: Remove leader election from Redis store — DONE

Deleted 3 methods (`acquire_scheduler_lock`, `refresh_scheduler_lock`, `release_scheduler_lock`) and the `SCHEDULER_LOCK_KEY`/`SCHEDULER_LOCK_TTL` constants.

## Step 2: Remove chat-only methods from Redis store — DONE

Deleted 3 chat/Slack methods (`set_thread_mapping`, `get_session_for_thread`, `remove_thread_mapping`) and 2 pub/sub methods (`publish_agent_event`, `subscribe_agent_events`) plus all subscriber task lifecycle code. Also deleted constants: `THREAD_HASH_KEY`, `AGENT_EVENTS_CHANNEL`.

**Note:** `publish_agent_event` and `subscribe_agent_events` DID exist in the copied file (contrary to the original plan note) — they were removed.

## Step 3: Update Redis key prefix — DONE

All key prefixes changed from `votebot:` to `ddp:`:
- `ACTIVE_JURISDICTIONS_KEY = "ddp:active_jurisdictions"`
- `SYNC_CHECKPOINT_PREFIX = "ddp:sync:checkpoint:"`
- `BILL_VERSION_PREFIX = "ddp:bill_version:"`
- `SYNC_TASK_PREFIX = "ddp:sync:task:"`

## Step 4: Remove chat-only methods from webflow_lookup.py — DONE

Rewrote `webflow_lookup.py` to keep only:
- `WebflowLookupService.__init__()` — simplified, uses plain `str` keys
- `update_bill_fields()` — PATCH CMS fields
- `update_bill_gov_url()` — thin wrapper

Removed everything else: 5 chat-only public methods, ~10 private helper methods (`_fetch_bill_by_id`, `_fetch_bill_by_slug`, `_resolve_org_references`, `_fetch_org_by_id`, `_fetch_org_item_by_id`, `_fetch_org_item_by_slug`, `_resolve_bill_references`, `_fetch_bill_info_by_id`, `_fetch_legislator_by_id`, `_fetch_legislator_by_slug`), 7 result dataclasses, 5 format functions.

**Plan correction:** `fetch_jurisdiction_mapping()` and `_fetch_collection_items()` were listed as "keep" methods but they do NOT exist in the file — they were never part of `webflow_lookup.py`. Jurisdiction mapping is handled directly in `bill_sync.py` and `openstates.py` via httpx calls. No impact.

## Step 5: vector_store.py — No changes needed (as planned)

All methods are used by sync. Confirmed no chat-specific methods exist.

## Step 6: Zombie watchdog — No changes needed (as planned)

Already runs unconditionally, uses `ddp:sync:task:*` prefix.

## Step 7: Health check — No changes needed (as planned)

Already fixed in Phase 1.

## Step 8 (NEW): Fix `.get_secret_value()` calls across codebase

SyncSettings uses plain `str` fields, not pydantic `SecretStr`. All `.get_secret_value()` calls copied from VoteBot would fail at runtime with `AttributeError: 'str' object has no attribute 'get_secret_value'`.

**Fixed in 14 files:**
- `services/vector_store.py` — `pinecone_api_key`
- `services/embeddings.py` — `openai_api_key`
- `services/webflow_lookup.py` — `webflow_votebot_api_key` (rewritten)
- `sync/federal_legislator_cache.py` — `openstates_api_key`
- `sync/handlers/bill.py` — `webflow_votebot_api_key` (2 occurrences)
- `sync/handlers/legislator.py` — `webflow_votebot_api_key`
- `ingestion/sources/webflow.py` — `webflow_votebot_api_key`
- `ingestion/sources/openstates.py` — `openstates_api_key`, `webflow_votebot_api_key`
- `ingestion/sources/congress.py` — `congress_api_key`
- `pipelines/bill_sync.py` — `openstates_api_key`, `webflow_votebot_api_key`
- `pipelines/bill_version.py` — `webflow_scheduler_api_key` (2 occurrences)
- `pipelines/legislator_sync.py` — `openstates_api_key`, `webflow_votebot_api_key`
- `pipelines/organization_sync.py` — `webflow_votebot_api_key` (2 occurrences)
- `pipelines/change_detection.py` — `congress_api_key`, `openstates_api_key`
- `scheduler.py` — `webflow_votebot_api_key`

## Step 9 (NEW): Add missing `webflow_votebot_api_key` to config

Many files reference `self.settings.webflow_votebot_api_key` but the field was never added to `SyncSettings`. Added:
- `webflow_votebot_api_key: str = ""` field in `SyncSettings`
- `"webflow_votebot_api_key": os.getenv("WEBFLOW_VOTEBOT_API_KEY", "")` in `_load_from_env()`

This key must also be added to the `ddp-sync/credentials` Secrets Manager secret during deployment (Phase 7).

---

## Verification results

All 9 checks passed:

- [x] `redis_store.py` has no `acquire_scheduler_lock`, `refresh_scheduler_lock`, `release_scheduler_lock` methods
- [x] `redis_store.py` has no `set_thread_mapping`, `get_session_for_thread`, `remove_thread_mapping` methods
- [x] `redis_store.py` has no `publish_agent_event`, `subscribe_agent_events` methods
- [x] `webflow_lookup.py` has no `get_bill_org_positions`, `get_org_bill_positions`, or detail lookup methods
- [x] All Redis keys use `ddp:` prefix (no `votebot:` references remain)
- [x] No `.get_secret_value()` calls remain in any file
- [x] `config.py` has `webflow_votebot_api_key` field
- [x] Full import chain works (app.py → scheduler → routes → pipelines → sync handlers)
- [x] All Python files compile without syntax errors

---

## Already done (from Phase 1 verification)

- [x] Health endpoint uses `scheduler.scheduler` (not `_scheduler`) — fixed in Phase 1
- [x] `get_scheduler()` function exists in `scheduler.py` — added in Phase 1
- [x] Zombie watchdog runs unconditionally (no leader gating) — implemented this way from start
- [x] Watchdog uses `ddp:sync:task:*` key prefix — set during Phase 1

---

## Files modified (this phase)

| File | Change | Lines |
|------|--------|-------|
| `src/ddp_sync/services/redis_store.py` | Rewrote: removed 8 methods (leader election + chat + pub/sub), renamed key prefixes | 362 → 218 (-144) |
| `src/ddp_sync/services/webflow_lookup.py` | Rewrote: removed 5 public + ~10 private methods, 7 dataclasses, 5 format functions | 1,126 → 107 (-1,019) |
| `src/ddp_sync/config.py` | Added `webflow_votebot_api_key` field + env mapping | +3 |
| 14 files across codebase | Removed `.get_secret_value()` calls, fixed merged-line syntax errors | ~-22 calls |

**No changes needed:** `app.py` (watchdog already simplified), `health.py` (already fixed in Phase 1)

**Net result:** 1,163 lines removed from the two main files. All runtime `AttributeError`s from `SecretStr` migration prevented.
