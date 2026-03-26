# Phase 2: Move DDP-API Scheduled Jobs into ddp-sync

*Part of: DDP-Sync Migration (see PLAN-move-sync-to-own-service.md for overview)*
*Depends on: Phase 1 complete and running*

## Goal

Extract all scheduled jobs from DDP-API's `scheduler.py` (1,202 lines) into ddp-sync. After this phase, ddp-sync runs all 11 scheduled jobs (3 VoteBot + 8 DDP-API), and DDP-API no longer starts a scheduler.

---

## What moves

DDP-API's `scheduler.py` contains two groups of functions:

### Group A: Voatz/Brevo sync (lines 54–989, ~935 lines)

| Function | Lines | Purpose |
|---|---|---|
| `clean_email()` | 54–73 | Email normalization via `email-validator` |
| `get_state_code_from_precinct()` | 76–87 | Parse "FLORIDA-SEM-7-38-10" → "FL" |
| `is_us_phone_number()` | 90–125 | US vs international phone detection |
| `get_voatz_tokens()` | 128–147 | Voatz API login → WS + CSRF tokens |
| `fetch_voatz_users()` | 150–190 | Paginated Voatz user fetch |
| `fetch_brevo_contacts()` | 193–236 | Paginated Brevo contact list fetch |
| `flatten_voatz_user()` | 239–275 | Flatten nested Voatz user struct |
| `add_contacts_to_brevo()` | 276–412 | Batch Brevo add with phone conflict resolution |
| `remove_contacts_from_brevo()` | 413–458 | Batch unlink contacts from Brevo list |
| `clear_phone_from_brevo_contact()` | 459–537 | Clear WHATSAPP attribute |
| `resolve_phone_ownership()` | 538–605 | Phone conflict resolution across orgs |
| `sync_org()` | 606–745 | Diff-based sync for one org |
| `full_sync_org()` | 748–835 | Full re-import for one org |
| `push_alert_to_zapier()` | 836–868 | POST sync summary to Zapier |
| `run_sync_job()` | 871–919 | Orchestrator: incremental sync all orgs |
| `run_full_sync_job()` | 922–989 | Orchestrator: full sync all orgs |

### Group B: Webflow CMS batch ops (lines 996–1097, ~100 lines)

| Function | Lines | Purpose |
|---|---|---|
| `_get_webflow_client()` | 996–1010 | Factory: `WebflowClient` + collection IDs from config |
| `run_webflow_fill_session_code()` | 1013–1024 | Fill session-code/bill-prefix/bill-number |
| `run_webflow_fill_map_url()` | 1027–1038 | Fill map-url + set visibility |
| `run_webflow_bill_org_sync()` | 1041–1052 | Sync bill↔org references |
| `run_webflow_org_about_parse()` | 1055–1066 | Parse org about-fields |
| `run_webflow_check_org_missing()` | 1069–1081 | Audit missing org fields + Zapier |
| `run_webflow_find_duplicates()` | 1084–1097 | Detect duplicate/companion bills |

---

## Step 1: Create `src/ddp_sync/pipelines/voatz_brevo.py`

Move Group A functions. Key changes:

1. **Config access**: DDP-API uses `from config import get_config` returning a dict. ddp-sync uses `from ddp_sync.config import get_settings` returning a `SyncSettings` dataclass. The Voatz/Brevo code accesses config as a dict (e.g., `config.get("brevo_api_key")`), so we need a bridge.

```python
# src/ddp_sync/pipelines/voatz_brevo.py
"""
Voatz → Brevo user sync pipeline.

Moved from DDP-API scheduler.py. Syncs user data from Voatz to Brevo
contact lists, with phone conflict resolution and overseas detection.
"""

import logging
import requests
import time
from email_validator import validate_email, EmailNotValidError

from ddp_sync.config import get_settings

logger = logging.getLogger(__name__)

# Voatz API endpoints (unchanged from DDP-API)
LOGIN_URL = "https://vapi-vrb.nimsim.com/api/v1/user/loginWithCredentials"
USERS_URL = "https://vapi-vrb.nimsim.com/api/v1/org/memberManagementList"
LOGIN_HEADERS = {"accept": "application/json", "Content-Type": "application/json"}

STATE_CODES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    # ... (full dict from scheduler.py lines 20-52)
}

OVERSEAS_LIST_ID = 58


def _get_org_config() -> list[dict]:
    """Get org configs, merging root-level brevo_api_key and blacklist."""
    settings = get_settings()
    orgs = settings.organizations or []
    result = []
    for org in orgs:
        merged = dict(org)
        merged.setdefault("brevo_api_key", settings.brevo_api_key)
        merged.setdefault("blacklist", settings.blacklist)
        result.append(merged)
    return result


# --- All functions below are copied verbatim from DDP-API scheduler.py ---
# Only change: replace `config = get_config()` with `settings = get_settings()`
# where config is loaded at the top of orchestrator functions.

def clean_email(email: str) -> str | None:
    # ... (lines 54-73, unchanged)
    pass

def get_state_code_from_precinct(precinct: str) -> str | None:
    # ... (lines 76-87, unchanged)
    pass

def is_us_phone_number(phone: str) -> bool:
    # ... (lines 90-125, unchanged)
    pass

def get_voatz_tokens(email, password, org_id):
    # ... (lines 128-147, unchanged)
    pass

def fetch_voatz_users(ws_token, csrf_token, org_id):
    # ... (lines 150-190, unchanged)
    pass

def fetch_brevo_contacts(api_key, list_id):
    # ... (lines 193-236, unchanged)
    pass

def flatten_voatz_user(user: dict) -> dict:
    # ... (lines 239-275, unchanged)
    pass

def add_contacts_to_brevo(api_key, list_id, users, claimed_phones, brevo_phones):
    # ... (lines 276-412, unchanged)
    pass

def remove_contacts_from_brevo(api_key, list_id, emails):
    # ... (lines 413-458, unchanged)
    pass

def clear_phone_from_brevo_contact(api_key, phone):
    # ... (lines 459-537, unchanged)
    pass

def resolve_phone_ownership(api_key, phone, new_email, claimed_phones, brevo_phones):
    # ... (lines 538-605, unchanged)
    pass

def sync_org(org_config, claimed_phones=None):
    # ... (lines 606-745, unchanged)
    pass

def full_sync_org(org_config, claimed_phones=None):
    # ... (lines 748-835, unchanged)
    pass

def push_alert_to_zapier(webhook_url, summaries):
    # ... (lines 836-868, unchanged)
    pass


def run_sync_job():
    """Incremental sync: diff Voatz users against Brevo, add/remove as needed."""
    settings = get_settings()
    orgs = _get_org_config()

    # Sort so Federal runs last (largest list, most phone conflicts)
    orgs.sort(key=lambda o: o["name"] == "Federal")

    summaries = []
    claimed_phones = {}

    for org in orgs:
        try:
            result = sync_org(org, claimed_phones=claimed_phones)
            if result:
                summaries.append(result)
        except Exception as e:
            logger.error(f"Sync failed for {org['name']}: {e}")

    if summaries and settings.zapier_webhook_url:
        push_alert_to_zapier(settings.zapier_webhook_url, summaries)


def run_full_sync_job():
    """Full-attribute sync: re-import all users for all orgs."""
    settings = get_settings()
    orgs = _get_org_config()
    orgs.sort(key=lambda o: o["name"] == "Federal")

    claimed_phones = {}
    for org in orgs:
        try:
            full_sync_org(org, claimed_phones=claimed_phones)
        except Exception as e:
            logger.error(f"Full sync failed for {org['name']}: {e}")
```

**Note:** The actual functions are copied verbatim — they use `requests` (sync HTTP), not `httpx` (async). This is fine because APScheduler runs them in a thread pool. No async conversion needed.

## Step 2: Create `src/ddp_sync/pipelines/webflow_batch.py`

Move Group B functions. These are thin wrappers around the `webflow_cms` package.

```python
# src/ddp_sync/pipelines/webflow_batch.py
"""
Webflow CMS batch operations.

Moved from DDP-API scheduler.py. Weekly maintenance jobs that fill
missing CMS fields, sync references, and detect duplicates.
"""

import logging

from ddp_sync.config import get_settings

logger = logging.getLogger(__name__)


def _get_webflow_client():
    """Instantiate WebflowClient with config from SyncSettings."""
    from webflow_cms import WebflowClient
    settings = get_settings()

    token = settings.webflow_api_token
    if not token:
        logger.error("webflow_api_token not configured")
        return None, "", ""

    return (
        WebflowClient(token),
        settings.webflow_bills_collection_id,
        settings.webflow_organizations_collection_id,
    )


def run_webflow_fill_session_code():
    """Fill session-code, bill-prefix, bill-number from open-states-url-2."""
    client, bills_cid, _ = _get_webflow_client()
    if not client:
        return
    try:
        from webflow_cms.services.fill_session_code import SessionCodeService
        service = SessionCodeService(client)
        result = service.fill(bills_cid)
        logger.info(f"Fill session-code: {result.items_updated} updated, "
                     f"{result.items_already_filled} already filled")
    except Exception as e:
        logger.error(f"Fill session-code failed: {e}")


def run_webflow_fill_map_url():
    """Fill map-url and set bill visibility."""
    client, bills_cid, _ = _get_webflow_client()
    if not client:
        return
    try:
        from webflow_cms.services.fill_map_url import MapUrlService
        service = MapUrlService(client)
        result = service.fill(bills_cid)
        logger.info(f"Fill map-url: {result.items_updated} updated")
    except Exception as e:
        logger.error(f"Fill map-url failed: {e}")


def run_webflow_bill_org_sync():
    """Sync bill-org references."""
    client, bills_cid, orgs_cid = _get_webflow_client()
    if not client:
        return
    try:
        from webflow_cms.services.bill_org_sync import BillOrgSyncService
        service = BillOrgSyncService(client)
        result = service.sync_bill_org_references(bills_cid, orgs_cid)
        logger.info(f"Bill-org sync: {result.orgs_updated} orgs updated, "
                     f"{result.references_added} refs added")
    except Exception as e:
        logger.error(f"Bill-org sync failed: {e}")


def run_webflow_org_about_parse():
    """Parse about-organization into sub-fields."""
    client, _, orgs_cid = _get_webflow_client()
    if not client:
        return
    try:
        from webflow_cms.services.bill_org_sync import BillOrgSyncService
        service = BillOrgSyncService(client)
        updated = service.parse_about_fields(orgs_cid)
        logger.info(f"Org about-field parse: {updated} updated")
    except Exception as e:
        logger.error(f"Org about-field parse failed: {e}")


def run_webflow_check_org_missing():
    """Check organizations for missing fields, send Zapier hooks."""
    client, _, orgs_cid = _get_webflow_client()
    if not client:
        return
    try:
        from webflow_cms.services.bill_org_sync import BillOrgSyncService
        settings = get_settings()
        service = BillOrgSyncService(client)
        results = service.check_missing_fields(
            orgs_cid,
            fields_to_check=["about-organization", "website-link"],
            send_zapier_hooks=bool(settings.zapier_webhook_url),
        )
        logger.info(f"Org missing fields check: {len(results)} orgs with issues")
    except Exception as e:
        logger.error(f"Org missing fields check failed: {e}")


def run_webflow_find_duplicates():
    """Find duplicate and companion bills (report only)."""
    client, bills_cid, _ = _get_webflow_client()
    if not client:
        return
    try:
        from webflow_cms.services.duplicate_bills import DuplicateBillsService
        service = DuplicateBillsService(client)
        groups = service.find_duplicates(bills_cid)
        dupes = [g for g in groups if g.group_type == "duplicate"]
        companions = [g for g in groups if g.group_type == "companion"]
        logger.info(f"Duplicate scan: {len(dupes)} duplicate groups, "
                     f"{len(companions)} companion groups")
    except Exception as e:
        logger.error(f"Duplicate scan failed: {e}")
```

## Step 3: Add trigger API routes

```python
# src/ddp_sync/api/routes/triggers.py
"""On-demand trigger endpoints for scheduled jobs."""

import logging
from fastapi import APIRouter, Depends, HTTPException
from ddp_sync.api.auth import api_key_auth

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/trigger/user-sync")
async def trigger_user_sync(token: str = Depends(api_key_auth)):
    """Trigger incremental Voatz → Brevo user sync."""
    try:
        from ddp_sync.pipelines.voatz_brevo import run_sync_job
        # Run in thread pool (sync function)
        import asyncio
        await asyncio.get_event_loop().run_in_executor(None, run_sync_job)
        return {"status": "completed", "job": "user_sync"}
    except Exception as e:
        logger.error(f"User sync trigger failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/trigger/full-sync")
async def trigger_full_sync(token: str = Depends(api_key_auth)):
    """Trigger full-attribute Voatz → Brevo sync."""
    try:
        from ddp_sync.pipelines.voatz_brevo import run_full_sync_job
        import asyncio
        await asyncio.get_event_loop().run_in_executor(None, run_full_sync_job)
        return {"status": "completed", "job": "full_sync"}
    except Exception as e:
        logger.error(f"Full sync trigger failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
```

**Note:** `run_sync_job()` and `run_full_sync_job()` are synchronous (they use `requests`). We run them in a thread pool via `run_in_executor` so they don't block the async event loop. This matches how APScheduler's `BackgroundScheduler` ran them in DDP-API.

## Step 4: Register DDP-API jobs in scheduler

Update `src/ddp_sync/scheduler.py` to add the 8 DDP-API jobs:

```python
# Add to SyncScheduler._register_jobs():

def _register_jobs(self):
    # ... (Phase 1 jobs: bill_version_sync, legislator_sync, organization_sync)

    # --- Phase 2 jobs (from DDP-API) ---

    # Voatz → Brevo user sync — every N minutes (default 30)
    from ddp_sync.pipelines.voatz_brevo import run_sync_job, run_full_sync_job
    self._scheduler.add_job(
        run_sync_job,
        IntervalTrigger(minutes=self.settings.sync_interval_minutes),
        id="voatz_user_sync",
        name="Voatz → Brevo user sync",
    )

    # Voatz → Brevo full-attribute sync — monthly 1st at 02:00 UTC
    self._scheduler.add_job(
        run_full_sync_job,
        CronTrigger(day=1, hour=2),
        id="voatz_full_sync",
        name="Voatz → Brevo full-attribute sync",
    )

    # Webflow CMS batch jobs — weekly Monday at 03:00 UTC
    from ddp_sync.pipelines.webflow_batch import (
        run_webflow_fill_session_code,
        run_webflow_fill_map_url,
        run_webflow_bill_org_sync,
        run_webflow_org_about_parse,
        run_webflow_check_org_missing,
        run_webflow_find_duplicates,
    )

    webflow_jobs = [
        ("webflow_fill_session_code", "Webflow: fill session-code", run_webflow_fill_session_code),
        ("webflow_fill_map_url", "Webflow: fill map-url", run_webflow_fill_map_url),
        ("webflow_bill_org_sync", "Webflow: bill-org reference sync", run_webflow_bill_org_sync),
        ("webflow_org_about_parse", "Webflow: org about-field parse", run_webflow_org_about_parse),
        ("webflow_check_org_missing", "Webflow: check org missing fields", run_webflow_check_org_missing),
        ("webflow_find_duplicates", "Webflow: find duplicate bills", run_webflow_find_duplicates),
    ]

    for job_id, name, func in webflow_jobs:
        self._scheduler.add_job(
            func,
            CronTrigger(day_of_week="mon", hour=3),
            id=job_id,
            name=name,
        )
```

**Important:** The Voatz/Brevo and Webflow batch functions are synchronous. With `AsyncIOScheduler`, sync functions are automatically run in the default thread pool executor. No manual `run_in_executor` wrapping needed for scheduler jobs.

## Step 5: Register trigger routes in app

```python
# Update src/ddp_sync/app.py create_app():

def create_app() -> FastAPI:
    # ...
    from ddp_sync.api.routes.health import router as health_router
    from ddp_sync.api.routes.sync_unified import router as sync_router
    from ddp_sync.api.routes.triggers import router as trigger_router

    app.include_router(health_router, prefix=API_PREFIX)
    app.include_router(sync_router, prefix=API_PREFIX)
    app.include_router(trigger_router, prefix=API_PREFIX)

    return app
```

## Step 6: Handle `run_sync_job()` immediate-on-startup behavior

DDP-API's `start_scheduler()` calls `run_sync_job()` immediately after starting (line 1191). This means every DDP-API restart triggers an immediate Voatz→Brevo sync.

**Decision:** Don't replicate this in ddp-sync. The scheduled job runs every 30 minutes — waiting at most 30 minutes after a restart is acceptable. If an immediate sync is needed, use the trigger endpoint.

---

## Verification checklist

### Code verification (local — all passed)

- [x] All 3 new files created: `voatz_brevo.py` (973 lines), `webflow_batch.py` (120 lines), `triggers.py` (64 lines)
- [x] All internal imports verified — 45 Python files, 0 unresolved
- [x] App creation succeeds — 8 API routes under `/ddp-sync/v1/`
- [x] Pipeline modules import cleanly — 17 functions from `voatz_brevo`, 7 from `webflow_batch`
- [x] Scheduler `_register_ddp_api_jobs()` method exists and registers 8 jobs
- [x] All 3 trigger routes have `api_key_auth` dependency
- [x] Pure function tests passed: `clean_email`, `get_state_code_from_precinct`, `is_us_phone_number`, `flatten_voatz_user`
- [x] `_get_org_configs()` correctly merges root-level `brevo_api_key` and `blacklist`
- [x] Federal-last sorting verified (state orgs claim phones first)
- [x] DDP-API parity check: constants match (URLs, STATE_CODES, OVERSEAS_LIST_ID), all function signatures match
- [x] Pushed to GitHub (`Digital-Democracy-Project/ddp-sync`)

### Runtime verification (requires EC2 deployment — Phase 7)

- [ ] `GET /ddp-sync/v1/schedule` shows all 11 jobs with correct schedules
- [ ] `POST /ddp-sync/v1/trigger/user-sync` completes successfully, Brevo contacts updated
- [ ] `POST /ddp-sync/v1/trigger/full-sync` completes successfully
- [ ] Webflow batch job runs on schedule and updates CMS (requires `webflow-cms` package)
- [ ] Zapier webhook fires after sync if summaries exist
- [ ] All 9 organizations sync correctly (Federal runs last)
- [ ] Phone conflict resolution works across orgs (shared `claimed_phones` dict)

---

## Files created/modified (this phase)

| File | Lines (est.) | Action |
|------|---|---|
| `src/ddp_sync/pipelines/voatz_brevo.py` | 973 | New (moved from DDP-API scheduler.py) |
| `src/ddp_sync/pipelines/webflow_batch.py` | 120 | New (moved from DDP-API scheduler.py) |
| `src/ddp_sync/api/routes/triggers.py` | 64 | New |
| `src/ddp_sync/scheduler.py` | +55 | Modified (add `_register_ddp_api_jobs()`) |
| `src/ddp_sync/app.py` | +2 | Modified (register trigger router) |
