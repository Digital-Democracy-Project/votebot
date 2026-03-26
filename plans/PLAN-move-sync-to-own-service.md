# Plan: Create Unified DDP-Sync Service

*Last updated: March 11, 2026 — All phases COMPLETE (1-8)*

## Motivation

The DDP platform currently has sync/scheduler logic scattered across two services:

**In VoteBot (`:8000`):**
- Bill version sync (OpenStates → Webflow CMS + Pinecone) — daily 04:00 UTC
- Legislator sync (OpenStates → Pinecone) — weekly Sun 06:00 UTC
- Organization sync (Webflow → Pinecone) — monthly 1st 08:00 UTC
- Zombie sync watchdog, checkpoint/resume, leader election
- On-demand sync API (unified sync, status polling, legacy single-item endpoints)

**In DDP-API (`:5000`):**
- Voatz → Brevo user sync — every 30 min
- Voatz → Brevo full-attribute sync — monthly 1st at 02:00 UTC
- Webflow CMS batch jobs (6 jobs) — weekly Mon 03:00 UTC
  - Fill session-code/bill-prefix/bill-number
  - Fill map-url and set visibility
  - Sync bill-org references
  - Parse org about-fields
  - Check org missing fields
  - Find duplicate/companion bills

None of this is "VoteBot" or "API proxy" work — it's all **data pipeline orchestration**. Co-locating it with the services it supports causes:

1. **OOM kills during sync crash chat workers** — HR 7148 (1,540-page PDF) killed the VoteBot worker serving user chat
2. **Complex crash recovery in VoteBot** — zombie watchdog, heartbeat tracking, checkpoint/resume, and leader election all exist because sync shares a process with the chat API
3. **Operational coupling** — restarting DDP-API for a config change kills a running Voatz sync; restarting VoteBot for a chat fix kills a running bill sync
4. **Silent sync failures are hard to diagnose** — sync logging is interleaved with chat/proxy traffic in journalctl
5. **Two independent schedulers** — APScheduler in both VoteBot and DDP-API, with no coordination or unified view of what's running
6. **Config sprawl** — secrets split across `.env` files, `config.local.json`, and AWS Secrets Manager with no consistent pattern

A single **ddp-sync** service in its **own repository** with **AWS Secrets Manager as the primary config source** eliminates all six problems.

---

## Architecture Overview

### Current (scattered)

```
nginx (:80/443)
  └─► DDP-API (:5000, 2 uvicorn workers)
        ├── API proxy routes (Voatz, Brevo, Webflow, VoteBot)
        ├── Scheduler (APScheduler)
        │     ├── Voatz → Brevo user sync (every 30 min)
        │     ├── Voatz → Brevo full-attribute sync (monthly)
        │     └── Webflow CMS batch jobs (6 jobs, weekly)
        │
        └─► VoteBot (:8000, 2 uvicorn workers)
              ├── Chat routes (WebSocket, HTTP)
              ├── Sync routes (unified, legacy)
              ├── Scheduler (APScheduler, leader-elected)
              │     ├── Bill version sync (daily)
              │     ├── Legislator sync (weekly)
              │     └── Organization sync (monthly)
              ├── Zombie watchdog
              └── Ingestion pipeline
```

### Proposed (unified)

```
nginx (:80/443)
  └─► DDP-API (:5000, 2 uvicorn workers)
        ├── API proxy only (Voatz, Brevo, Webflow, VoteBot chat, sync)
        ├── No scheduler, no background jobs
        └── Routes sync requests to ddp-sync (:8001)

  VoteBot (:8000, 2 uvicorn workers)
        ├── Chat routes (WebSocket, HTTP)
        ├── Health check
        └── Pinecone search (read-only)

  DDP-Sync (:8001, 1 uvicorn worker)          ◄── NEW REPO: ddp-sync
        ├── Unified Scheduler (single APScheduler)
        │     ├── Voatz → Brevo user sync (every 30 min)
        │     ├── Voatz → Brevo full-attribute sync (monthly)
        │     ├── Webflow CMS batch jobs (weekly)
        │     ├── Bill version sync: OpenStates → Webflow + Pinecone (daily)
        │     ├── Legislator sync: OpenStates → Pinecone (weekly)
        │     └── Organization sync: Webflow → Pinecone (monthly)
        ├── Sync API (unified sync, status polling, triggers)
        ├── Ingestion pipeline (Pinecone write, OpenAI embeddings)
        ├── Zombie sync watchdog (simplified, no leader election)
        └── Health check
```

**Key changes:**
- **DDP-Sync lives in its own repo** (`ddp-sync`) — independent release cycle, own dependencies
- **VoteBot** becomes a pure **chat/RAG service** — no sync, no scheduler, no ingestion
- **DDP-API** remains the **auth gateway + API proxy** — routes requests to external APIs (Voatz, Brevo, Webflow CMS) and internal services (VoteBot, ddp-sync). No scheduler, no background jobs. As new internal services are added, DDP-API gains new proxy routes
- **DDP-Sync** is the **data pipeline service** — scheduled/batch jobs, on-demand triggers for those pipelines, and the heavy compute (embeddings, Pinecone writes, PDF processing)
- **AWS Secrets Manager** is the single source of truth for all service credentials
- Single APScheduler instance — no leader election needed (1 worker)
- systemd manages all three services independently (`Restart=on-failure`)

---

## Repository Structure

### New repo: `ddp-sync`

```
ddp-sync/
├── pyproject.toml                          # Package definition + all dependencies
├── .env.example                            # Local dev env template
├── infrastructure/
│   └── ddp-sync.service                    # systemd unit file
├── config/
│   └── sync_schedule.yaml                  # Scheduler config (moved from votebot)
├── src/ddp_sync/
│   ├── __init__.py
│   ├── app.py                              # FastAPI app with lifespan
│   ├── config.py                           # Unified config: AWS Secrets Manager → env fallback
│   ├── scheduler.py                        # Unified APScheduler (all 11 jobs)
│   │
│   ├── pipelines/                          # Data pipeline modules
│   │   ├── __init__.py
│   │   ├── bill_version.py                 # OpenStates → Webflow + Pinecone (from votebot)
│   │   ├── legislator.py                   # OpenStates → Pinecone (from votebot)
│   │   ├── organization.py                 # Webflow → Pinecone (from votebot)
│   │   ├── voatz_brevo.py                  # Voatz → Brevo (from ddp-api scheduler.py)
│   │   └── webflow_batch.py                # Webflow CMS ops (from ddp-api scheduler.py)
│   │
│   ├── ingestion/                          # Moved from votebot
│   │   ├── pipeline.py                     # Chunking, embedding, upsert
│   │   ├── metadata.py
│   │   └── sources/
│   │       ├── webflow.py
│   │       ├── openstates.py
│   │       └── pdf.py
│   │
│   ├── services/                           # Shared service clients
│   │   ├── redis_store.py                  # Redis (sync keys only)
│   │   ├── vector_store.py                 # Pinecone client
│   │   ├── webflow_lookup.py               # Webflow CMS read/write
│   │   ├── embeddings.py                   # OpenAI embeddings
│   │   └── legislative_calendar.py         # Session detection (from votebot utils/)
│   │
│   ├── api/                                # HTTP API
│   │   ├── routes/
│   │   │   ├── sync_unified.py             # POST /sync/unified, GET /status/{id}
│   │   │   ├── triggers.py                 # POST /trigger/user-sync, /trigger/full-sync
│   │   │   └── health.py                   # GET /health, GET /schedule
│   │   └── auth.py                         # API key validation
│   │
│   └── sync/                               # Sync orchestration (from votebot)
│       ├── handlers/
│       │   ├── bill.py
│       │   ├── legislator.py
│       │   └── organization.py
│       ├── service.py
│       └── types.py
```

### Relationship to VoteBot

DDP-Sync **does not depend on the `votebot` package**. The sync/ingestion code is moved (not imported) into the new repo. This gives ddp-sync:

- Independent releases — sync fixes don't require a VoteBot deploy
- Own dependency tree — no risk of chat deps conflicting with sync deps
- Clean ownership — all data pipeline code in one place

The shared service modules (`redis_store.py`, `vector_store.py`, `webflow_lookup.py`) are copied into ddp-sync and can diverge over time. They're stable, small files (~200-400 lines each) that rarely change. If drift becomes a problem, extract into a `ddp-common` package later.

---

## Configuration: AWS Secrets Manager

### Current config landscape (fragmented)

| Service | Config Source | What's There |
|---------|-------------|--------------|
| VoteBot | `.env` file (pydantic-settings) | Pinecone, OpenAI, Redis, Webflow, OpenStates, Slack keys |
| DDP-API | AWS Secrets Manager `ddp-api/org-credentials` → `config.local.json` → env | Voatz creds, Brevo keys, Webflow token, org list, blacklists |
| DDP-API | `.env` file | `API_BEARER_TOKEN`, `VOTEBOT_SERVICE_URL`, `VOTEBOT_API_KEY` |

### Proposed: AWS Secrets Manager as primary

**New secret: `ddp-sync/credentials`**

```json
{
  "_comment": "DDP-Sync credentials — AWS Secrets Manager: ddp-sync/credentials",

  "api_key": "",

  "openai_api_key": "",
  "openai_embedding_model": "text-embedding-3-large",

  "pinecone_api_key": "",
  "pinecone_environment": "us-east-1",
  "pinecone_index_name": "votebot-large",
  "pinecone_namespace": "default",

  "openstates_api_key": "",
  "congress_api_key": "",

  "redis_url": "redis://localhost:6379/0",

  "webflow_scheduler_api_key": "",
  "webflow_api_token": "",
  "webflow_site_id": "",
  "webflow_bills_collection_id": "",
  "webflow_jurisdiction_collection_id": "",
  "webflow_legislators_collection_id": "",
  "webflow_categories_collection_id": "",
  "webflow_organizations_collection_id": "",

  "brevo_api_key": "",
  "brevo_rate_limit_rph": 36000,
  "blacklist": [],
  "zapier_webhook_url": "",
  "sync_interval_minutes": 30,
  "organizations": [
    {
      "name": "Federal",
      "voatz_email": "",
      "voatz_password": "",
      "voatz_org_id": 0,
      "voatz_creator_id": 0,
      "brevo_list_id": 0
    },
    {
      "name": "Arizona",
      "voatz_email": "",
      "voatz_password": "",
      "voatz_org_id": 0,
      "voatz_creator_id": 0,
      "brevo_list_id": 0
    }
  ]
}
```

**Field sources:**

| Field(s) | Current Source | Notes |
|----------|--------------|-------|
| `api_key` | VoteBot `.env` `API_KEY` | Authenticates DDP-API → ddp-sync requests |
| `openai_api_key`, `openai_embedding_model` | VoteBot `.env` | Embeddings during ingestion |
| `pinecone_*` (including `pinecone_namespace`) | VoteBot `.env` | Vector store writes |
| `openstates_api_key` | VoteBot `.env` | Bill/legislator data |
| `congress_api_key` | VoteBot `.env` | Congress.gov change detection + federal bill ingestion |
| `redis_url` | VoteBot `.env` | Task state, checkpoints, version cache |
| `webflow_scheduler_api_key` | VoteBot `.env` `WEBFLOW_SCHEDULER_API_KEY` | Read+write token (status/gov-url PATCH, bill ingestion reads) |
| `webflow_api_token` | DDP-API `.env` `WEBFLOW_API_TOKEN` | Token for `webflow_cms` package (batch fill, bill-org sync, duplicates) |
| `webflow_*_collection_id` | VoteBot `.env` + DDP-API `.env` | Collection IDs (verify DDP-API's `WEBFLOW_COLLECTION_ID` == VoteBot's `WEBFLOW_BILLS_COLLECTION_ID`) |
| `brevo_api_key`, `brevo_rate_limit_rph`, `blacklist` | DDP-API `.env` + `ddp-api/org-credentials` | Root-level Brevo defaults + rate limiting |
| `zapier_webhook_url` | DDP-API `ddp-api/org-credentials` | Post-sync alerts |
| `sync_interval_minutes` | DDP-API `ddp-api/org-credentials` | Voatz user sync frequency |
| `organizations[]` | DDP-API `ddp-api/org-credentials` | Per-org Voatz creds + Brevo list IDs. `voatz_creator_id` used for Voatz event creation (future: replace Zapier tasks). 9 orgs: Federal, AZ, FL, MA, MI, NV, UT, VA, WA |

**Create with AWS CLI:**
```bash
aws secretsmanager create-secret \
  --name ddp-sync/credentials \
  --secret-string file://ddp-sync-secret.json \
  --region us-east-1
```

Or via AWS console: Secrets Manager → Store a new secret → Other type of secret → Plaintext → paste the JSON.

This consolidates all sync-related credentials from both VoteBot `.env` and DDP-API's `ddp-api/org-credentials` into one secret.

**Config loader (`src/ddp_sync/config.py`):**

```python
"""
Configuration for DDP-Sync.

Priority: AWS Secrets Manager → .env file → defaults.
Production uses Secrets Manager. Local dev uses .env.
"""

import json
import os
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

AWS_SECRET_NAME = os.getenv("AWS_SECRET_NAME", "ddp-sync/credentials")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")


def _load_from_secrets_manager() -> dict | None:
    try:
        import boto3
        client = boto3.client("secretsmanager", region_name=AWS_REGION)
        response = client.get_secret_value(SecretId=AWS_SECRET_NAME)
        config = json.loads(response["SecretString"])
        logger.info(f"Loaded config from Secrets Manager: {AWS_SECRET_NAME}")
        return config
    except Exception as e:
        logger.warning(f"Secrets Manager unavailable: {e}")
        return None


def _load_from_env() -> dict:
    """Fallback: build config dict from environment variables."""
    from dotenv import load_dotenv
    load_dotenv()
    return {
        "api_key": os.getenv("DDP_SYNC_API_KEY", os.getenv("VOTEBOT_API_KEY", "")),

        "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
        "openai_embedding_model": os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large"),

        "pinecone_api_key": os.getenv("PINECONE_API_KEY", ""),
        "pinecone_environment": os.getenv("PINECONE_ENVIRONMENT", "us-east-1"),
        "pinecone_index_name": os.getenv("PINECONE_INDEX_NAME", "votebot-large"),
        "pinecone_namespace": os.getenv("PINECONE_NAMESPACE", "default"),

        "openstates_api_key": os.getenv("OPENSTATES_API_KEY", ""),
        "congress_api_key": os.getenv("CONGRESS_API_KEY", ""),

        "redis_url": os.getenv("REDIS_URL", "redis://localhost:6379/0"),

        "webflow_scheduler_api_key": os.getenv("WEBFLOW_SCHEDULER_API_KEY", ""),
        "webflow_api_token": os.getenv("WEBFLOW_API_TOKEN", ""),
        "webflow_site_id": os.getenv("WEBFLOW_SITE_ID", ""),
        "webflow_bills_collection_id": os.getenv("WEBFLOW_BILLS_COLLECTION_ID", ""),
        "webflow_jurisdiction_collection_id": os.getenv("WEBFLOW_JURISDICTION_COLLECTION_ID", ""),
        "webflow_legislators_collection_id": os.getenv("WEBFLOW_LEGISLATORS_COLLECTION_ID", ""),
        "webflow_categories_collection_id": os.getenv("WEBFLOW_CATEGORIES_COLLECTION_ID", ""),
        "webflow_organizations_collection_id": os.getenv("WEBFLOW_ORGANIZATIONS_COLLECTION_ID", ""),

        "brevo_api_key": os.getenv("BREVO_API_KEY", ""),
        "brevo_rate_limit_rph": int(os.getenv("BREVO_RATE_LIMIT_RPH", "36000")),
        "blacklist": [],
        "zapier_webhook_url": os.getenv("ZAPIER_WEBHOOK_URL", ""),
        "sync_interval_minutes": int(os.getenv("SYNC_INTERVAL_MINUTES", "30")),
    }


@lru_cache
def get_config() -> dict:
    """Load config: Secrets Manager first, .env fallback."""
    config = _load_from_secrets_manager()
    if config is None:
        config = _load_from_env()
    return config
```

**Local dev `.env.example`:**
```bash
# DDP-Sync local development config
# Copy to .env and fill in values. Production uses AWS Secrets Manager.

DDP_SYNC_API_KEY=
OPENAI_API_KEY=
OPENAI_EMBEDDING_MODEL=text-embedding-3-large
PINECONE_API_KEY=
PINECONE_ENVIRONMENT=us-east-1
PINECONE_INDEX_NAME=votebot-large
PINECONE_NAMESPACE=default
OPENSTATES_API_KEY=
CONGRESS_API_KEY=
REDIS_URL=redis://localhost:6379/0
WEBFLOW_SCHEDULER_API_KEY=
WEBFLOW_API_TOKEN=
WEBFLOW_SITE_ID=
WEBFLOW_BILLS_COLLECTION_ID=
WEBFLOW_JURISDICTION_COLLECTION_ID=
WEBFLOW_LEGISLATORS_COLLECTION_ID=
WEBFLOW_CATEGORIES_COLLECTION_ID=
WEBFLOW_ORGANIZATIONS_COLLECTION_ID=
BREVO_API_KEY=
BREVO_RATE_LIMIT_RPH=36000
ZAPIER_WEBHOOK_URL=
SYNC_INTERVAL_MINUTES=30
```

**Benefits:**
- One place to update credentials (AWS console or CLI)
- No `.env` files to keep in sync across services on EC2
- Automatic secret rotation support (future)
- Local dev still works with `.env` fallback
- DDP-API's existing `ddp-api/org-credentials` secret is unchanged — ddp-sync reads its own secret

**Migration path for existing secrets:**
1. Create `ddp-sync/credentials` in AWS Secrets Manager
2. Populate from VoteBot's `.env` + DDP-API's `ddp-api/org-credentials`
3. DDP-Sync reads from Secrets Manager on EC2, `.env` locally
4. VoteBot and DDP-API configs unchanged (they keep their own sources)

### What DDP-API needs to know about DDP-Sync

Add to **existing** `ddp-api/org-credentials` secret:

```json
{
  "ddp_sync_service_url": "http://localhost:8001",
  "ddp_sync_api_key": "..."
}
```

This is the only change to DDP-API's config — two keys for routing sync requests.

---

## What Moves Where

### From VoteBot → DDP-Sync (new repo)

| Code | Purpose |
|------|---------|
| `src/votebot/sync/` | Handlers, service, types |
| `src/votebot/updates/` | Bill version sync, legislator sync, org sync, scheduler |
| `src/votebot/ingestion/` | Pipeline, chunking, metadata, sources (Webflow, PDF, OpenStates) |
| `src/votebot/utils/legislative_calendar.py` | Session detection |
| `src/votebot/services/embeddings.py` | OpenAI embeddings (copied — both repos need it; VoteBot uses `embed_query()` at chat time) |
| `src/votebot/services/redis_store.py` | Redis client (copy — sync key namespaces) |
| `src/votebot/services/vector_store.py` | Pinecone client (copy — sync uses write path) |
| `src/votebot/services/webflow_lookup.py` | Webflow CMS (copy — sync uses write methods) |
| `src/votebot/api/routes/sync_unified.py` | Unified sync API |
| `src/votebot/api/routes/sync.py` | Legacy sync endpoints (dropped, not migrated) |
| `config/sync_schedule.yaml` | Scheduler config |

### From DDP-API → DDP-Sync (new repo)

| Code | Purpose |
|------|---------|
| `scheduler.py` (1202 lines) | All scheduled jobs: Voatz→Brevo sync, Webflow CMS batch ops |

### Stays in VoteBot (chat-only)

```
src/votebot/api/routes/chat.py             # HTTP chat
src/votebot/api/routes/websocket.py        # WebSocket chat
src/votebot/api/routes/content.py          # URL→context resolution (Webflow reads, no sync deps)
src/votebot/api/routes/health.py           # Health check
src/votebot/core/agent.py                  # LLM agent
src/votebot/core/retrieval.py              # RAG retrieval (zero sync/updates imports)
src/votebot/core/prompts.py                # System prompts
src/votebot/services/bill_votes.py         # OpenStates vote lookup (query-time)
src/votebot/services/llm.py                # Claude API
src/votebot/services/web_search.py         # Web search
src/votebot/services/slack.py              # Slack integration
src/votebot/services/query_logger.py       # Query logging
src/votebot/services/embeddings.py          # OpenAI embeddings (KEPT — used by vector_store.py for embed_query() at chat time)
src/votebot/services/redis_store.py        # Redis (kept — chat keys: threads, pub/sub)
src/votebot/services/vector_store.py       # Pinecone (kept — chat reads)
src/votebot/services/webflow_lookup.py     # Webflow CMS (kept — chat reads positions)
src/votebot/utils/federal_legislator_cache.py  # Moved from sync/ (used by retrieval.py)
```

### Stays in DDP-API (proxy-only)

```
app/routes/voatz.py        # Voatz API proxy (user-triggered: get_tokens, get_users, get_events)
app/routes/brevo.py        # Brevo API proxy (update_segment, user_updates)
app/routes/votebot.py      # VoteBot chat proxy + sync proxy (re-routed to :8001)
app/routes/webflow.py      # Webflow CMS on-demand endpoints (fill, check, resolve, delete)
app/routes/sync.py         # trigger_sync, trigger_full_sync (re-routed to :8001)
app/middleware/auth.py      # Bearer token validation
```

> **Note on shared service copies**: `redis_store.py`, `vector_store.py`, and `webflow_lookup.py` exist in both VoteBot and ddp-sync. They're stable modules (~200-400 lines each) that rarely change. If drift becomes a maintenance issue, extract into a `ddp-common` package later.

---

## Dependency Analysis

### VoteBot: Zero circular dependencies confirmed

| Direction | Imports | Count |
|-----------|---------|-------|
| Chat → Sync | None | 0 |
| Sync → Chat | None | 0 |

*`federal_legislator_cache.py` lives in `sync/` but has zero sync-layer imports. Move to `utils/` during migration.*

### DDP-API scheduler: Self-contained

The scheduler.py (1202 lines) imports only:
- Standard library (`logging`, `requests`, `time`, `datetime`)
- `email_validator`
- `apscheduler`
- `config.get_config()` (config loader)
- `webflow_cms` (Webflow CMS package)

No imports from DDP-API's route handlers or middleware. Clean extraction.

### DDP-Sync dependency set

```toml
# pyproject.toml [project.dependencies]
dependencies = [
    # Web framework
    "fastapi>=0.109.0",
    "uvicorn[standard]>=0.27.0",

    # Scheduler
    "APScheduler>=3.10.4",

    # Vector store + embeddings (bill/legislator/org ingestion)
    "pinecone-client>=3.0.0",
    "openai>=1.10.0",

    # Data extraction
    "pdfplumber>=0.10.0",
    "beautifulsoup4>=4.12.0",

    # External APIs
    "httpx>=0.26.0",
    "requests>=2.31.0",

    # Config + infra
    "boto3>=1.34.0",
    "redis>=5.0.0",
    "python-dotenv>=1.0.0",
    "structlog>=24.1.0",
    "pyyaml>=6.0",

    # Voatz/Brevo pipeline
    "email-validator>=2.0.0",

    # Webflow CMS batch ops
    "webflow-cms>=1.0.0",
]
```

### Redis key namespaces (no conflicts)

| Current Key | New Key (ddp-sync) | Used By | Purpose |
|-------------|---------------------|---------|---------|
| `votebot:sync:task:{id}` | `ddp:sync:task:{id}` | DDP-Sync | Task state, heartbeat |
| `votebot:sync:checkpoint:{id}` | `ddp:sync:checkpoint:{id}` | DDP-Sync | Resume tracking |
| `votebot:bill_version:{id}` | `ddp:bill_version:{id}` | DDP-Sync | Version cache (90-day TTL) |
| `votebot:active_jurisdictions` | `ddp:active_jurisdictions` | DDP-Sync | Discovered states |
| `votebot:scheduler:leader` | Removed | — | No longer needed (single worker) |
| `votebot:threads` | (unchanged) | VoteBot | Slack thread mapping |
| `votebot:agent_events` | (unchanged) | VoteBot | Pub/sub for handoff |

**Redis key migration:** During Wave 1 deployment, run a one-time script to copy existing `votebot:sync:*` and `votebot:bill_version:*` keys to the new `ddp:*` prefix. This preserves version cache (avoids re-checking all bills) and any in-flight task state. Old keys can be left to expire naturally (24h for tasks, 90 days for version cache).

---

## Implementation Plan

### Phase 1: Create ddp-sync repo with VoteBot sync code -- COMPLETE

**Goal:** New repo with standalone FastAPI service running VoteBot's sync/scheduler.
**Status:** Implemented and verified. Repo: `Digital-Democracy-Project/ddp-sync`. 42 Python files, all imports rewritten, app creation and config loading verified locally. Remaining items (uvicorn startup, endpoint tests) require Redis (EC2 deployment in Phase 7).

1. Create `ddp-sync` repo on GitHub (org: `Digital-Democracy-Project`)
2. Set up `pyproject.toml` with dependency list above
3. Move sync code from VoteBot into `src/ddp_sync/`:
   - `sync/`, `updates/`, `ingestion/` → `pipelines/`, `ingestion/`, `sync/`
   - Copy shared services (`redis_store.py`, `vector_store.py`, `webflow_lookup.py`, `embeddings.py`)
   - Move `legislative_calendar.py` → `services/`
   - Move `sync_schedule.yaml` → `config/`
   - Move `sync_unified.py` routes → `api/routes/`
4. Create `src/ddp_sync/config.py` — AWS Secrets Manager → `.env` fallback
5. Create `src/ddp_sync/app.py` — FastAPI with unified lifespan (scheduler, Redis, watchdog)
6. Create `ddp-sync/credentials` secret in AWS Secrets Manager
7. Update imports throughout (change `votebot.*` → `ddp_sync.*`)

**Endpoints:**

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/ddp-sync/v1/sync/unified` | Trigger batch or single sync (bill, legislator, or org) |
| `GET` | `/ddp-sync/v1/sync/unified/status/{task_id}` | Poll task status |
| `POST` | `/ddp-sync/v1/sync/unified/all` | Trigger sync for all content types |
| `POST` | `/ddp-sync/v1/trigger/user-sync` | Trigger Voatz→Brevo sync (Phase 2) |
| `POST` | `/ddp-sync/v1/trigger/full-sync` | Trigger full-attribute sync (Phase 2) |
| `POST` | `/ddp-sync/v1/trigger/webflow/{job_name}` | Trigger specific Webflow batch job (Phase 2) |
| `GET` | `/ddp-sync/v1/health` | Health check |
| `GET` | `/ddp-sync/v1/schedule` | Show all scheduled jobs and next run times |

**Legacy endpoints dropped:** The old single-item endpoints (`POST /sync/bill`, `/sync/legislator`, `/sync/organization`) are not carried forward. They duplicate `/sync/unified` with `"mode": "single"`. DDP-API's proxy handlers for these are also removed.

**Scheduler jobs (Phase 1):**

| Job | Schedule | Source |
|-----|----------|--------|
| Bill version sync | Daily 04:00 UTC | VoteBot `updates/scheduler.py` |
| Legislator sync | Weekly Sun 06:00 UTC | VoteBot `updates/scheduler.py` |
| Organization sync | Monthly 1st 08:00 UTC | VoteBot `updates/scheduler.py` |

### Phase 2: Move DDP-API scheduled jobs into ddp-sync -- COMPLETE

**Goal:** Consolidate all scheduled jobs under one scheduler.
**Status:** Implemented. 3 new files (voatz_brevo.py, webflow_batch.py, triggers.py), 8 DDP-API jobs registered in scheduler, 3 trigger endpoints added. 45 total Python files, all imports verified.

**Extract `scheduler.py` functions from DDP-API into ddp-sync:**

```
src/ddp_sync/pipelines/
    voatz_brevo.py      # run_sync_job(), run_full_sync_job(), sync_org(), full_sync_org(),
                        # fetch_voatz_users(), add/remove_contacts_to_brevo(),
                        # phone conflict resolution
    webflow_batch.py    # run_webflow_fill_session_code(), run_webflow_fill_map_url(),
                        # run_webflow_bill_org_sync(), run_webflow_org_about_parse(),
                        # run_webflow_check_org_missing(), run_webflow_find_duplicates()
```

**Config:** The Voatz org credentials (username, password, Brevo list IDs, blacklists) are already populated in `ddp-sync/credentials` from Phase 1. The `voatz_organizations` array in the secret replaces DDP-API's per-org config structure.

**Updated scheduler (all jobs):**

| Job | Schedule | Origin |
|-----|----------|--------|
| Voatz → Brevo user sync | Every 30 min (configurable) | DDP-API |
| Voatz → Brevo full-attribute sync | Monthly 1st 02:00 UTC | DDP-API |
| Webflow fill session-code | Weekly Mon 03:00 UTC | DDP-API |
| Webflow fill map-url | Weekly Mon 03:00 UTC | DDP-API |
| Webflow bill-org reference sync | Weekly Mon 03:00 UTC | DDP-API |
| Webflow org about-field parse | Weekly Mon 03:00 UTC | DDP-API |
| Webflow check org missing fields | Weekly Mon 03:00 UTC | DDP-API |
| Webflow find duplicate bills | Weekly Mon 03:00 UTC | DDP-API |
| Bill version sync (OpenStates → Webflow + Pinecone) | Daily 04:00 UTC | VoteBot |
| Legislator sync (OpenStates → Pinecone) | Weekly Sun 06:00 UTC | VoteBot |
| Organization sync (Webflow → Pinecone) | Monthly 1st 08:00 UTC | VoteBot |

### Phase 3: Simplify ddp-sync internals — COMPLETE

**Status:** Implemented and verified. 17 files changed, +45/-1,207 lines.

**Completed:**
- Removed 8 methods from `redis_store.py` (leader election, chat/Slack thread mapping, pub/sub): 362 → 218 lines
- Stripped `webflow_lookup.py` to sync-only methods (`update_bill_fields`, `update_bill_gov_url`): 1,126 → 107 lines
- Renamed all Redis key prefixes: `votebot:` → `ddp:`
- Added missing `webflow_votebot_api_key` field to `SyncSettings` + env var mapping

**Critical fix discovered:** Removed `.get_secret_value()` calls from 14 files across the codebase. VoteBot uses pydantic `SecretStr`; ddp-sync uses plain `str`. All calls would have caused `AttributeError` at runtime on EC2.

**Note:** `webflow_votebot_api_key` must be added to the `ddp-sync/credentials` Secrets Manager secret during Phase 7 deployment (same value as VoteBot's `WEBFLOW_VOTEBOT_API_KEY`).

**Unified health check:**
```json
{
  "status": "healthy",
  "service": "ddp-sync",
  "config_source": "secrets_manager",
  "scheduler": {
    "running": true,
    "jobs": 11,
    "next_run": "2026-03-11T03:00:00Z"
  },
  "redis": "connected",
  "pinecone": "connected"
}
```

### Phase 4: Remove sync code from VoteBot — COMPLETE

**Status:** Implemented and verified. Commit `df25db5` pushed to `main`. 37 files changed, +8/-14,571 lines.

**Completed:**
- Simplified `src/votebot/main.py` (356 → 131 lines): removed scheduler, leader election, watchdog, zombie sync resume
- Removed sync route registrations (`sync_unified_router`, `sync_router`)
- Moved `federal_legislator_cache.py` from `sync/` to `utils/`, updated import in `core/retrieval.py`
- Removed `scheduler_enabled` field from `config.py`
- Removed 4 sync-only deps from `pyproject.toml` (`tiktoken`, `PyPDF2`, `pdfplumber`, `apscheduler`)
- Deleted `sync/`, `updates/`, `ingestion/` directories, sync API routes, sync schemas, scheduler config

**Discovery: `embeddings.py` must be KEPT in VoteBot.** `vector_store.py` imports `EmbeddingService` for `embed_query()` — used at chat time to embed user queries before Pinecone search. Both VoteBot and ddp-sync need their own copy.

**Deleted sync-only code from VoteBot:**
- `src/votebot/sync/` (entire directory)
- `src/votebot/updates/` (entire directory)
- `src/votebot/ingestion/` (entire directory)
- `src/votebot/api/routes/sync_unified.py` (542 lines)
- `src/votebot/api/routes/sync.py` (460 lines)
- `src/votebot/api/schemas/sync.py` (51 lines)
- `config/sync_schedule.yaml` (123 lines)

### Phase 5: Remove scheduler from DDP-API — COMPLETE

**Status:** Implemented and verified. Commit `94175e4` pushed to DDP-API `main`. 6 files changed, +11/-1,666 lines.

**Completed:**
- Simplified `app/main.py` (103 → 79 lines): removed scheduler start/stop from lifespan, removed `sync_router`
- Deleted `scheduler.py` (1,202 lines), `app/routes/sync.py` (37 lines), `tests/test_phone_conflict.py` (386 lines)
- Removed `APScheduler` and `email-validator` from `requirements.txt` (10 → 8 deps)
- DDP-API is now a pure auth gateway + API proxy with zero background jobs

**Note:** `app/routes/votebot.py` still has 5 sync proxy handlers that forward to VoteBot (which now returns 404). Phase 6 replaces these with the catch-all proxy to ddp-sync.

### Phase 6: Update DDP-API proxy routing

#### Catch-all proxy pattern

Instead of writing per-endpoint proxy handlers (which requires a DDP-API code change for every new ddp-sync endpoint), use a **catch-all proxy route**. This means adding new ddp-sync endpoints is a single-repo change — just add the endpoint in ddp-sync, and DDP-API forwards it automatically.

**New file: `app/routes/ddp_sync_proxy.py`:**

```python
"""Catch-all proxy for DDP-Sync service.

All requests to /sync/* and /trigger/* are forwarded to ddp-sync.
New ddp-sync endpoints are automatically available — no DDP-API changes needed.
"""

import os
import httpx
from fastapi import APIRouter, Depends, Request, Response
from app.middleware.auth import bearer_auth

router = APIRouter()


def _get_ddp_sync_config() -> dict:
    try:
        from config import get_config
        config = get_config()
        return {
            "service_url": config.get(
                "ddp_sync_service_url",
                os.getenv("DDP_SYNC_SERVICE_URL", "http://localhost:8001"),
            ),
            "api_key": config.get(
                "ddp_sync_api_key",
                os.getenv("DDP_SYNC_API_KEY", ""),
            ),
        }
    except Exception:
        return {
            "service_url": os.getenv("DDP_SYNC_SERVICE_URL", "http://localhost:8001"),
            "api_key": os.getenv("DDP_SYNC_API_KEY", ""),
        }


@router.api_route("/sync/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_sync(request: Request, path: str, token: str = Depends(bearer_auth)):
    """Forward /sync/* to ddp-sync."""
    return await _forward_to_ddp_sync(request, f"sync/{path}")


@router.api_route("/trigger/{path:path}", methods=["GET", "POST"])
async def proxy_trigger(request: Request, path: str, token: str = Depends(bearer_auth)):
    """Forward /trigger/* to ddp-sync."""
    return await _forward_to_ddp_sync(request, f"trigger/{path}")


async def _forward_to_ddp_sync(request: Request, path: str) -> Response:
    config = _get_ddp_sync_config()
    timeout = 300.0 if request.method == "POST" else 30.0
    async with httpx.AsyncClient(base_url=config["service_url"], timeout=timeout) as client:
        response = await client.request(
            method=request.method,
            url=f"/ddp-sync/v1/{path}",
            headers={"Authorization": f"Bearer {config['api_key']}"},
            content=await request.body(),
            params=request.query_params,
        )
        return Response(
            content=response.content,
            status_code=response.status_code,
            media_type=response.headers.get("content-type"),
        )
```

**Register in `app/main.py`:**
```python
from app.routes.ddp_sync_proxy import router as ddp_sync_router
app.include_router(ddp_sync_router, prefix="/votebot")  # /votebot/sync/*, /votebot/trigger/*
```

#### Route mapping after migration

```
DDP-API External Path                    → Backend           Internal Path
─────────────────────────────────────────────────────────────────────────────────────
POST /votebot/chat                       → VoteBot :8000     /votebot/v1/chat
POST /votebot/chat/stream                → VoteBot :8000     /votebot/v1/chat/stream
POST /votebot/feedback                   → VoteBot :8000     /votebot/v1/chat/feedback
WS   /votebot/ws                         → VoteBot :8000     ws://.../ws/chat
*    /votebot/sync/*                     → DDP-Sync :8001    /ddp-sync/v1/sync/*         (catch-all)
*    /votebot/trigger/*                  → DDP-Sync :8001    /ddp-sync/v1/trigger/*      (catch-all)
POST /trigger_sync                       → DDP-Sync :8001    /ddp-sync/v1/trigger/user-sync
POST /trigger_full_sync                  → DDP-Sync :8001    /ddp-sync/v1/trigger/full-sync
```

Legacy single-item sync endpoints (`/votebot/sync/bill`, `/sync/legislator`, `/sync/organization`) are removed. Callers should use `/votebot/sync/unified` with `"mode": "single"` instead.

**External paths do NOT change** — DDP website and API consumers are unaffected.

**Adding new ddp-sync endpoints:** Just add the route in ddp-sync under `/ddp-sync/v1/sync/*` or `/ddp-sync/v1/trigger/*`. The catch-all proxy forwards it automatically. No DDP-API code change, no DDP-API redeploy. This is key for incrementally replacing Zapier tasks — each new pipeline is a single ddp-sync PR.

#### Code cleanup in DDP-API

- **Delete** per-endpoint sync handlers from `app/routes/votebot.py` (the 2 unified sync handlers + 3 legacy handlers)
- **Delete** `app/routes/sync.py` (trigger endpoints replaced by catch-all proxy)
- **Add** `app/routes/ddp_sync_proxy.py` (catch-all proxy, ~60 lines)

#### Config: Add to existing `ddp-api/org-credentials` secret

```json
{
  "ddp_sync_service_url": "http://localhost:8001",
  "ddp_sync_api_key": "..."
}
```

### Phase 7: Deployment

**New systemd service: `ddp-sync.service`**

```ini
[Unit]
Description=DDP Sync Service
After=network.target redis-server.service
Wants=redis-server.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/ddp-sync
Environment=PATH=/home/ubuntu/ddp-sync/.venv/bin:/usr/bin
ExecStart=/home/ubuntu/ddp-sync/.venv/bin/uvicorn ddp_sync.app:app --host 0.0.0.0 --port 8001 --workers 1
Restart=on-failure
RestartSec=10
MemoryMax=2G

[Install]
WantedBy=multi-user.target
```

**Key design choices:**
- **Own working directory** (`/home/ubuntu/ddp-sync`) — separate from votebot and DDP-API
- **Own virtualenv** (`/home/ubuntu/ddp-sync/.venv`) — isolated dependencies
- **1 worker** — no concurrent request handling needed, no leader election
- **Port 8001** — separate from chat (8000) and proxy (5000)
- **`MemoryMax=2G`** — systemd kills cleanly on OOM
- **`Restart=on-failure`** + **`RestartSec=10`** — auto-restart with delay

**Update `votebot.service`:**
- Remove `SCHEDULER_ENABLED=true` from environment

**Deployment commands:**
```bash
# Clone and set up ddp-sync
cd /home/ubuntu
git clone git@github.com:Digital-Democracy-Project/ddp-sync.git
cd ddp-sync
python3 -m venv .venv
.venv/bin/pip install -e .

# Install systemd service
sudo cp infrastructure/ddp-sync.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ddp-sync
sudo systemctl start ddp-sync

# Verify
sudo systemctl status ddp-sync
sudo journalctl -u ddp-sync -n 50 --no-pager
curl http://localhost:8001/ddp-sync/v1/health

# Update DDP-API (add routing keys to Secrets Manager, deploy code)
cd ~/DDP-API
git pull origin main
sudo systemctl restart ddp-api

# Restart VoteBot (now without scheduler)
cd ~/votebot
git pull origin main
sudo systemctl restart votebot
```

### Phase 8: Update monitoring and docs

**Logs (3 services, cleanly separated):**
- `sudo journalctl -u ddp-sync` — all sync/scheduler activity
- `sudo journalctl -u votebot` — chat/RAG only
- `sudo journalctl -u ddp-api` — proxy/routing only

**Health checks:**
- DDP-Sync: `GET http://localhost:8001/ddp-sync/v1/health`
- VoteBot: `GET http://localhost:8000/votebot/v1/health` (unchanged)
- DDP-API: `GET http://localhost:5000/health` (unchanged)

**Documentation updates:**
- ddp-sync `README.md` — architecture, deployment, config, scheduling
- VoteBot `README.md` — update architecture, remove sync docs
- VoteBot `docs/TROUBLESHOOTING.md` — update sync sections to reference ddp-sync
- VoteBot `docs/RAG_ARCHITECTURE.md` — update sync scheduling section
- DDP-API `README.md` — document scheduler removal, proxy routing changes

---

## What Gets Simpler

| Current Complexity | After Unification |
|-------------------|-------------------|
| Redis leader election (5-min TTL lock, refresh loop, follower re-election) in VoteBot | Removed — single process, scheduler starts unconditionally |
| Zombie watchdog with heartbeat tracking | Simplified — systemd `Restart=on-failure` handles process crashes; internal watchdog still catches task-level stalls |
| 2 independent APScheduler instances (VoteBot + DDP-API) with no visibility into each other | 1 scheduler with all 11 jobs, single `/schedule` endpoint shows everything |
| OOM in sync kills chat workers | Isolated — sync OOM only affects ddp-sync; `MemoryMax=2G` provides clean systemd kill |
| `gc.collect()` every bill to protect chat memory | Still useful for sync, but chat is no longer at risk |
| 2 VoteBot workers fighting over who runs the scheduler | 1 dedicated sync worker, no contention |
| Sync logs interleaved with chat traffic (VoteBot) and proxy traffic (DDP-API) | Clean separation: `journalctl -u ddp-sync` shows only sync logs |
| VoteBot `main.py` is ~355 lines (>200 for scheduler/watchdog) | Chat `main.py` dropped to 131 lines (Phase 4 complete) |
| DDP-API restart kills running Voatz sync | Voatz sync runs in ddp-sync; DDP-API restart is safe |
| Config split across `.env`, `config.local.json`, AWS Secrets Manager | AWS Secrets Manager is primary; `.env` fallback for local dev only |
| Sync code tightly coupled to votebot package | Own repo, own release cycle, own dependency tree |

## What Gets More Complex

| New Complexity | Mitigation |
|----------------|------------|
| Three systemd services + three repos | Standard pattern; each service is simpler individually |
| Shared service code copied (redis_store, vector_store, webflow_lookup) | Phase 3 stripped ddp-sync copies to sync-only (webflow_lookup 1,126→107, redis_store 362→218). Minimal overlap remains; extract `ddp-common` if drift occurs |
| DDP-API routes to two backends | 2 sync + 2 trigger handlers re-routed; 3 legacy handlers deleted; chat handlers untouched |
| New AWS Secrets Manager secret to manage | AWS CLI or console; same pattern DDP-API already uses |
| Deploy touches three repos/services | Only restart what changed; most deploys touch only one |
| Import path changes during code move | One-time effort; search-replace `votebot.*` → `ddp_sync.*` |

---

## Migration Strategy

### Incremental rollout (recommended)

**Pre-deployment code work (Phases 1-4): COMPLETE**

All code changes are done and pushed to GitHub:
- Phases 1-2: ddp-sync repo created with all 11 jobs (`Digital-Democracy-Project/ddp-sync`)
- Phase 3: ddp-sync internals simplified (leader election removed, Redis keys renamed, `.get_secret_value()` fixed)
- Phase 4: VoteBot sync code removed (37 files, ~14,500 lines deleted). VoteBot is now chat-only

**Wave 1: Deploy ddp-sync + update routing (Phases 5, 6)**

1. Deploy ddp-sync on EC2 alongside existing services — `:8001`
2. Test ddp-sync endpoints directly — `curl localhost:8001/ddp-sync/v1/health`
3. Run Redis key migration script (`votebot:*` → `ddp:*`)
4. Deploy DDP-API with catch-all proxy (Phase 6) — sync traffic routes to ddp-sync
5. Deploy VoteBot (Phase 4 code already pushed) — now chat-only, no scheduler
6. Verify nightly bill sync runs from ddp-sync — check `journalctl -u ddp-sync` at 04:00 UTC

**Wave 2: Remove DDP-API scheduler (Phase 5)**

7. Verify ddp-sync has all 11 jobs running (`GET /ddp-sync/v1/schedule`)
8. Test Voatz→Brevo sync via ddp-sync trigger endpoint
9. Deploy DDP-API with scheduler removed
10. Verify no scheduler starts, proxy routes work

**Wave 3: Monitoring + docs (Phase 8)**

11. Update documentation across all three repos
12. Set up health check scripts and CloudWatch per-service monitoring

Waves 1-2 are independently valuable and fully reversible.

### Testing strategy (Wave 1)

Before cutting over DDP-API routing, verify ddp-sync end-to-end:

1. **Health check** — `curl localhost:8001/ddp-sync/v1/health` returns `config_source`, Redis and Pinecone status
2. **Single-bill sync** — `POST /ddp-sync/v1/sync/unified` with `{"type": "bill", "mode": "single", "webflow_id": "<known_bill>"}`. Verify:
   - Pinecone doc was written (check `document_type=bill` + `webflow_id` filter)
   - Webflow CMS `status` and `status-date` fields updated (or `webflow_skipped` if already current)
   - Task status returns `completed` via `GET /ddp-sync/v1/sync/unified/status/{task_id}`
3. **Single-legislator sync** — same flow with `{"type": "legislator", "mode": "single"}`. Verify Pinecone docs (profile, sponsored bills, voting records)
4. **Scheduler dry run** — `GET /ddp-sync/v1/schedule` shows all 3 Phase 1 jobs with correct next-run times
5. **Nightly bill sync (first night)** — After enabling scheduler, check `journalctl -u ddp-sync` at 04:00 UTC. Compare bill version cache in Redis (`ddp:bill_version:*`) against previous `votebot:bill_version:*` entries
6. **Chat regression** — After disabling VoteBot scheduler (`SCHEDULER_ENABLED=false`), run a few chat queries to confirm RAG retrieval still works (Pinecone reads unaffected)

### Testing strategy (Wave 2)

7. **Voatz→Brevo user sync** — `POST /ddp-sync/v1/trigger/user-sync`. Verify Brevo contact list updated, no duplicate phone conflicts
8. **Webflow batch job** — Trigger one batch job (e.g., fill session-code) via scheduler or API. Verify CMS items updated
9. **DDP-API proxy** — After updating trigger endpoints, call `POST /trigger_sync` through DDP-API and confirm it proxies to ddp-sync

**Rollback (Wave 1):** Set `ddp_sync_service_url` to `http://localhost:8000` in DDP-API's Secrets Manager and `SCHEDULER_ENABLED=true` in VoteBot, restart both.

**Rollback (Wave 2):** Re-enable `start_scheduler()` in DDP-API lifespan, revert trigger endpoints, restart DDP-API.

---

## Files Changed Summary

### New repo: `ddp-sync`

| File | Purpose |
|------|---------|
| `pyproject.toml` | Package definition, all dependencies |
| `src/ddp_sync/app.py` | FastAPI app with unified lifespan |
| `src/ddp_sync/config.py` | AWS Secrets Manager → .env config loader |
| `src/ddp_sync/scheduler.py` | Unified APScheduler (all 11 jobs) |
| `src/ddp_sync/pipelines/*.py` | Data pipeline modules (5 files) |
| `src/ddp_sync/ingestion/` | Ingestion pipeline (from votebot) |
| `src/ddp_sync/services/` | Redis (218 lines), Pinecone, Webflow CMS write-only (107 lines), embeddings, calendar |
| `src/ddp_sync/sync/` | Sync handlers and orchestration (from votebot) |
| `src/ddp_sync/api/` | HTTP API routes |
| `config/sync_schedule.yaml` | Scheduler config |
| `infrastructure/ddp-sync.service` | systemd unit file |
| AWS `ddp-sync/credentials` | Secrets Manager secret (includes `webflow_votebot_api_key` — added March 10, 2026) |

### Modified files (votebot repo) — Phase 4 COMPLETE

| File | Change |
|------|--------|
| `src/votebot/main.py` | 356 → 131 lines. Removed scheduler, watchdog, leader election, sync imports |
| `src/votebot/api/routes/__init__.py` | Remove `sync_router`, `sync_unified_router` imports and exports |
| `src/votebot/core/retrieval.py` | Update import path for `federal_legislator_cache` (`sync.` → `utils.`) |
| `src/votebot/config.py` | Remove `scheduler_enabled` field |
| `pyproject.toml` | Remove 4 sync-only deps: `tiktoken`, `PyPDF2`, `pdfplumber`, `apscheduler` |

### Deleted from votebot repo — Phase 4 COMPLETE (37 files, ~14,500 lines)

| Path | Reason |
|------|--------|
| `src/votebot/sync/` (entire directory) | Moved to ddp-sync (`federal_legislator_cache.py` moved to `utils/`) |
| `src/votebot/updates/` | Moved to ddp-sync |
| `src/votebot/ingestion/` | Moved to ddp-sync |
| `src/votebot/api/routes/sync_unified.py` (542 lines) | Moved to ddp-sync |
| `src/votebot/api/routes/sync.py` (460 lines) | Dropped (legacy endpoints) |
| `src/votebot/api/schemas/sync.py` (51 lines) | Sync request/response schemas, no longer needed |
| `config/sync_schedule.yaml` (123 lines) | Moved to ddp-sync |

**NOT deleted:** `src/votebot/services/embeddings.py` — originally planned for deletion but `vector_store.py` uses `EmbeddingService.embed_query()` at chat time for Pinecone searches. Both repos keep their own copy.

### Moved files (votebot repo)

| From | To | Reason |
|------|----|--------|
| `src/votebot/sync/federal_legislator_cache.py` | `src/votebot/utils/federal_legislator_cache.py` | Chat-time dependency; no sync imports |

### New files (ddp-api repo)

| File | Purpose |
|------|---------|
| `app/routes/ddp_sync_proxy.py` | Catch-all proxy: forwards `/sync/*` and `/trigger/*` to ddp-sync (~60 lines) |

### Modified files (ddp-api repo)

| File | Change |
|------|--------|
| `app/main.py` | Remove scheduler start/stop from lifespan; register `ddp_sync_router` |
| `app/routes/votebot.py` | Delete 5 sync handlers (2 unified + 3 legacy) — replaced by catch-all proxy |
| AWS `ddp-api/org-credentials` | Add `ddp_sync_service_url`, `ddp_sync_api_key` |

### Deleted from ddp-api repo

| File | Reason |
|------|--------|
| `scheduler.py` | All jobs moved to ddp-sync (Wave 2) |
| `app/routes/sync.py` | Trigger endpoints replaced by catch-all proxy |

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| DDP-Sync crashes, no sync runs | Low | Medium | systemd `Restart=on-failure` + `RestartSec=10` |
| AWS Secrets Manager unavailable | Very low | High | Config loader falls back to `.env`; keep `.env` on EC2 as emergency backup |
| Voatz/Brevo sync breaks during extraction | Medium | Medium | Test with `trigger_sync` endpoint before cutting over; Wave 2 is independent |
| Import path changes introduce bugs | Medium | Medium | Comprehensive testing before Wave 1 deploy; run sync on single bill first. **Note:** Phase 3 caught `.get_secret_value()` calls (VoteBot uses pydantic `SecretStr`, ddp-sync uses plain `str`) — fixed in 14 files |
| Shared service copies drift | Low | Low | Files are stable; extract `ddp-common` package if it becomes an issue |
| DDP-API mis-routes sync to VoteBot | Very low | Low | Each handler explicitly calls `get_ddp_sync_config()` or `get_votebot_config()` |
| Secret not populated correctly | Medium | Medium | Validate at startup — health check reports `config_source` and connection status |
| Redis contention | Very low | Low | Separate key namespaces, no overlapping writes |
| Rollback needed after partial deploy | Medium | Low | Each wave is independently reversible |

---

## Open Questions

1. ~~**Separate repo for ddp-sync?**~~ **Resolved:** Own repo (`ddp-sync`). Sync code moved, not imported. Shared services copied.

2. ~~**Separate API key?**~~ **Resolved:** Yes. `DDP_SYNC_API_KEY` (stored in Secrets Manager as `api_key`) gives access separation — sync endpoints can't be called with chat-only credentials. DDP-API authenticates to ddp-sync with `ddp_sync_api_key`.

3. ~~**Drop legacy sync endpoints?**~~ **Resolved:** Legacy single-item endpoints dropped. Use `/sync/unified` with `"mode": "single"` instead.

4. ~~**Config strategy?**~~ **Resolved:** AWS Secrets Manager (`ddp-sync/credentials`) as primary. `.env` fallback for local dev. DDP-API learns ddp-sync's URL via its own existing secret.

5. ~~**Should `/webflow/*` on-demand endpoints eventually move too?**~~ **Resolved:** No, keep in DDP-API. DDP-API's role is **auth gateway + API proxy** — it routes requests to external APIs (Voatz, Brevo, Webflow CMS) and internal services (VoteBot, ddp-sync). The `/webflow/*` endpoints are lightweight proxy calls to the `webflow_cms` package, consistent with DDP-API's existing Voatz and Brevo proxy endpoints. DDP-Sync's role is **scheduled/batch data pipelines** — it does the heavy lifting (embeddings, Pinecone writes, PDF processing) and exposes triggers for its own pipelines. The *scheduled* versions of some Webflow operations move to ddp-sync (Wave 2), but the user-triggered management endpoints stay in DDP-API.

6. ~~**Redis key prefix rename?**~~ **Resolved:** Rename `votebot:sync:*` → `ddp:sync:*` and `votebot:bill_version:*` → `ddp:bill_version:*` during Wave 1. One-time migration script copies existing keys; old keys expire naturally. VoteBot's `votebot:threads` and `votebot:agent_events` stay unchanged.

---

## Current State (as of March 10, 2026)

### Implementation progress

| Phase | Status | Key Outcome |
|-------|--------|-------------|
| Phase 1: Create ddp-sync repo | COMPLETE | 42 Python files, all imports rewritten, pushed to GitHub |
| Phase 2: Move DDP-API jobs | COMPLETE | 3 new files, 8 DDP-API jobs + 3 trigger endpoints added |
| Phase 3: Simplify internals | COMPLETE | -1,163 lines (redis_store, webflow_lookup), `.get_secret_value()` fixed in 14 files |
| Phase 4: Remove sync from VoteBot | COMPLETE | -14,571 lines, VoteBot is now chat-only (main.py: 131 lines) |
| Phase 5: Remove scheduler from DDP-API | COMPLETE | -1,666 lines, DDP-API is pure proxy (main.py: 79 lines) |
| Phase 6: DDP-API proxy routing | COMPLETE | Catch-all proxy for `/sync/*` and `/trigger/*`, 5 sync handlers removed from votebot.py |
| Phase 7: EC2 deployment | COMPLETE | Three-service architecture live. Python 3.11 installed, nginx updated, Redis migrated |
| Phase 8: Monitoring + docs | COMPLETE | READMEs updated across 3 repos, health check script, TROUBLESHOOTING.md updated |

### Discoveries during implementation

- **`embeddings.py` must stay in VoteBot** (Phase 4): `vector_store.py` imports `EmbeddingService` for `embed_query()` at chat time. Both repos need their own copy.
- **`.get_secret_value()` migration** (Phase 3): VoteBot uses pydantic `SecretStr`, ddp-sync uses plain `str`. 22+ calls across 14 files would have caused `AttributeError` at runtime. All fixed.
- **Missing `webflow_votebot_api_key`** (Phase 3): Field was referenced by many files but never in `SyncSettings`. Added to config + env loader. Already added to AWS Secrets Manager.
- **`api/schemas/sync.py` existed** (Phase 4): 51-line file with sync request/response models — not in original plan but deleted.
- **Dependency cleanup** (Phase 4): Removed `tiktoken`, `PyPDF2`, `pdfplumber`, `apscheduler` from VoteBot. Kept `tenacity` (used by llm.py, web_search.py) and `beautifulsoup4` (content resolution).
- **`test_phone_conflict.py` deleted** (Phase 5): 386-line test file that tested `scheduler.py` functions directly (`import scheduler`). These tests now belong in ddp-sync's test suite.
- **`middleware.py` is legacy Flask code** (Phase 5): Contains dead `scheduler` references but is never imported by the FastAPI app. Left as-is.
- **Catch-all proxy registered twice** (Phase 6): `ddp_sync_router` registered under `/votebot` prefix (for `/votebot/sync/*`, `/votebot/trigger/*`) and at root level (for `/trigger/*`, `/sync/*`). 29 total routes after Phase 6 (was 30 before — 5 sync handlers removed, 4 catch-all routes added).
- **`app/routes/sync.py` already deleted** (Phase 6): Was deleted in Phase 5 along with `scheduler.py`. Phase 6 plan originally listed it for deletion but it was already gone.
- **`votebot.py` 429 → 236 lines** (Phase 6): Exactly matched the plan estimate of ~238 lines. 193 lines of sync handlers removed.
- **Content-Type header not forwarded** (Phase 7): Catch-all proxy sent raw body bytes without Content-Type header, causing ddp-sync validation errors. Fixed in commit `69279dd`.
- **nginx HTTPS config had stale routing** (Phase 7): The SSL server block sent `/votebot/*` directly to VoteBot :8000, bypassing DDP-API. `/votebot/sync/` was already overridden to go to DDP-API, but `/votebot/trigger/` was not. Added explicit location. Also removed trailing slashes from location blocks to avoid 307 redirects.
- **Python 3.11 required on EC2** (Phase 7): EC2 had Python 3.10, ddp-sync requires `>=3.11`. Installed from deadsnakes PPA. VoteBot/DDP-API venvs unaffected (separate venvs).
- **Trigger endpoints are synchronous** (Phase 7): `/trigger/user-sync` and `/trigger/full-sync` block until completion. Should return task_id like `/sync/unified`. Backlogged in Phase 8.
- **Redis health check bug** (Phase 7): `RedisStore` health check accesses `self._redis` before lazy initialization. Cosmetic — Redis works for actual sync operations. Backlogged in Phase 8.
- **Memory well within t3a.medium** (Phase 7): All three services total ~309MB at startup. Staying on t3a.medium (4GB), monitoring before upgrading.

### Earlier audits

- **Dependency audit**: Confirmed zero circular imports between chat and sync in VoteBot. The only chat→sync dependency was `federal_legislator_cache.py` (moved to `utils/`).
- **DDP-API audit**: `scheduler.py` is self-contained (1202 lines). Imports only `requests`, `email_validator`, `apscheduler`, `config.get_config()`, `webflow_cms`. Clean extraction.
- **`webflow_lookup.py` write usage**: `update_bill_fields()` called only from sync code. Chat uses read-only lookups. After separation, write methods only in ddp-sync.
