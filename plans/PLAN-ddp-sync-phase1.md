# Phase 1: Create ddp-sync Repo with VoteBot Sync Code

*Part of: DDP-Sync Migration (see PLAN-move-sync-to-own-service.md for overview)*

## Goal

Create a new `ddp-sync` repository with a standalone FastAPI service that runs VoteBot's sync/scheduler jobs. At the end of this phase, ddp-sync starts on port 8001, runs the 3 VoteBot scheduled jobs (bill version sync, legislator sync, org sync), and exposes the unified sync API.

## Prerequisites

- GitHub repo `Digital-Democracy-Project/ddp-sync` created
- AWS Secrets Manager secret `ddp-sync/credentials` populated (see Phase 1 Step 6)

---

## Step 1: Create repository and project structure

```bash
mkdir ddp-sync && cd ddp-sync
git init
mkdir -p src/ddp_sync/{pipelines,ingestion/sources,services,sync/handlers,api/routes}
mkdir -p config infrastructure
touch src/ddp_sync/__init__.py
touch src/ddp_sync/pipelines/__init__.py
touch src/ddp_sync/ingestion/__init__.py
touch src/ddp_sync/ingestion/sources/__init__.py
touch src/ddp_sync/services/__init__.py
touch src/ddp_sync/sync/__init__.py
touch src/ddp_sync/sync/handlers/__init__.py
touch src/ddp_sync/api/__init__.py
touch src/ddp_sync/api/routes/__init__.py
```

## Step 2: pyproject.toml

```toml
[build-system]
requires = ["setuptools>=68.0", "wheel"]
build-backend = "setuptools.backends._legacy:_Backend"

[project]
name = "ddp-sync"
version = "0.1.0"
description = "DDP unified data pipeline service"
requires-python = ">=3.11"
dependencies = [
    # Web framework
    "fastapi>=0.109.0",
    "uvicorn[standard]>=0.27.0",

    # Scheduler
    "APScheduler>=3.10.4",

    # Vector store + embeddings
    "pinecone-client>=3.0.0",
    "openai>=1.10.0",

    # Data extraction
    "pdfplumber>=0.10.0",
    "beautifulsoup4>=4.12.0",
    "tiktoken>=0.5.0",

    # External APIs
    "httpx>=0.26.0",
    "requests>=2.31.0",

    # Config + infra
    "boto3>=1.34.0",
    "redis>=5.0.0",
    "python-dotenv>=1.0.0",
    "structlog>=24.1.0",
    "pyyaml>=6.0",

    # Voatz/Brevo pipeline (Phase 2)
    "email-validator>=2.0.0",

    # Webflow CMS batch ops (Phase 2)
    "webflow-cms>=1.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.0",
    "pytest-asyncio>=0.21",
    "ruff>=0.1.0",
]

[tool.setuptools.packages.find]
where = ["src"]
```

## Step 3: Config loader — `src/ddp_sync/config.py`

This replaces VoteBot's pydantic-settings `Settings` class. Every file in VoteBot's sync stack imports `from votebot.config import Settings, get_settings` — all of those will be rewritten to `from ddp_sync.config import get_config`.

The key difference: VoteBot uses a `Settings` dataclass with attribute access (`settings.pinecone_api_key`), while ddp-sync uses a plain dict (`config["pinecone_api_key"]`). This means every `settings.field_name` call in the moved code must be changed to `config["field_name"]` or we provide a thin wrapper.

**Recommended approach:** Use a `SyncSettings` dataclass that mirrors the fields the sync code actually uses, populated from the dict. This minimizes changes to the moved code.

```python
"""
Configuration for DDP-Sync.

Priority: AWS Secrets Manager → .env file → defaults.
Production uses Secrets Manager. Local dev uses .env.
"""

import json
import os
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)

AWS_SECRET_NAME = os.getenv("AWS_SECRET_NAME", "ddp-sync/credentials")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")


@dataclass
class SyncSettings:
    """Settings object for sync code. Mirrors fields from VoteBot's Settings
    that the sync/ingestion/updates code actually references."""

    # Auth
    api_key: str = ""

    # OpenAI
    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-large"

    # Pinecone
    pinecone_api_key: str = ""
    pinecone_environment: str = "us-east-1"
    pinecone_index_name: str = "votebot-large"
    pinecone_namespace: str = "default"

    # External APIs
    openstates_api_key: str = ""
    congress_api_key: str = ""

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Webflow
    webflow_scheduler_api_key: str = ""
    webflow_api_token: str = ""
    webflow_site_id: str = ""
    webflow_bills_collection_id: str = ""
    webflow_jurisdiction_collection_id: str = ""
    webflow_legislators_collection_id: str = ""
    webflow_categories_collection_id: str = ""
    webflow_organizations_collection_id: str = ""

    # Brevo / Voatz (Phase 2)
    brevo_api_key: str = ""
    brevo_rate_limit_rph: int = 36000
    blacklist: list = field(default_factory=list)
    zapier_webhook_url: str = ""
    sync_interval_minutes: int = 30
    organizations: list = field(default_factory=list)

    # Ingestion tuning (from VoteBot Settings)
    chunk_size: int = 1000
    chunk_overlap: int = 200
    pdf_max_pages: int = 1000

    # App
    environment: str = "production"
    debug: bool = False
    log_level: str = "INFO"


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
        "api_key": os.getenv("DDP_SYNC_API_KEY", ""),
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
        "chunk_size": int(os.getenv("CHUNK_SIZE", "1000")),
        "chunk_overlap": int(os.getenv("CHUNK_OVERLAP", "200")),
        "pdf_max_pages": int(os.getenv("PDF_MAX_PAGES", "1000")),
        "environment": os.getenv("ENVIRONMENT", "production"),
        "debug": os.getenv("DEBUG", "false").lower() == "true",
        "log_level": os.getenv("LOG_LEVEL", "INFO"),
    }


@lru_cache
def get_settings() -> SyncSettings:
    """Load config and return a SyncSettings instance.

    Named get_settings() so moved code that calls `from ddp_sync.config import get_settings`
    works with minimal changes (same function name as VoteBot's config).
    """
    raw = _load_from_secrets_manager()
    if raw is None:
        raw = _load_from_env()

    # Build SyncSettings from dict, ignoring unknown keys
    known_fields = {f.name for f in SyncSettings.__dataclass_fields__.values()}
    filtered = {k: v for k, v in raw.items() if k in known_fields}
    return SyncSettings(**filtered)


def get_config_source() -> str:
    """Return which config source was used (for health check)."""
    raw = _load_from_secrets_manager()
    return "secrets_manager" if raw is not None else "env"
```

**Why `get_settings()` instead of `get_config()`:** Every sync file imports `from votebot.config import get_settings`. By keeping the function name `get_settings`, the import rewrite is just changing the module path:

```python
# Before (in votebot)
from votebot.config import Settings, get_settings

# After (in ddp-sync) — only the module path changes
from ddp_sync.config import SyncSettings as Settings, get_settings
```

Or even simpler with an alias in `ddp_sync/config.py`:
```python
Settings = SyncSettings  # backward-compat alias
```

## Step 4: Move sync code from VoteBot

### Files to copy (with new paths)

**From `src/votebot/sync/` → `src/ddp_sync/sync/`:**

| VoteBot path | ddp-sync path | Lines | Notes |
|---|---|---|---|
| `sync/types.py` | `sync/types.py` | 145 | Zero votebot imports — copy as-is |
| `sync/service.py` | `sync/service.py` | 185 | Imports handlers + config |
| `sync/handlers/base.py` | `sync/handlers/base.py` | 51 | Imports only `sync.types` |
| `sync/handlers/bill.py` | `sync/handlers/bill.py` | 501 | Heavy imports: config, ingestion, redis, updates |
| `sync/handlers/legislator.py` | `sync/handlers/legislator.py` | 360 | Imports config, ingestion, updates |
| `sync/handlers/organization.py` | `sync/handlers/organization.py` | 280 | Imports config, ingestion, updates |
| `sync/handlers/webpage.py` | `sync/handlers/webpage.py` | 295 | Imports config, ingestion |
| `sync/handlers/training.py` | `sync/handlers/training.py` | 352 | Imports config, ingestion |
| `sync/build_legislator_votes.py` | `sync/build_legislator_votes.py` | 966 | Imports config, ingestion, vector_store, federal_legislator_cache |
| `sync/federal_legislator_cache.py` | `sync/federal_legislator_cache.py` | 375 | Imports only config |

**From `src/votebot/updates/` → `src/ddp_sync/pipelines/`:**

| VoteBot path | ddp-sync path | Lines | Notes |
|---|---|---|---|
| `updates/scheduler.py` | `scheduler.py` (top-level in ddp_sync) | 828 | Core scheduler — heaviest rewrites |
| `updates/bill_version_sync.py` | `pipelines/bill_version.py` | 850 | Deferred imports throughout |
| `updates/bill_sync.py` | `pipelines/bill_sync.py` | 1,450 | Largest file — config, ingestion, openstates, calendar |
| `updates/legislator_sync.py` | `pipelines/legislator_sync.py` | 1,205 | Config, ingestion |
| `updates/organization_sync.py` | `pipelines/organization_sync.py` | 435 | Config, ingestion |
| `updates/change_detection.py` | `pipelines/change_detection.py` | 266 | Config only |

**From `src/votebot/ingestion/` → `src/ddp_sync/ingestion/`:**

| VoteBot path | ddp-sync path | Lines | Notes |
|---|---|---|---|
| `ingestion/pipeline.py` | `ingestion/pipeline.py` | 419 | Config, chunking, metadata, vector_store |
| `ingestion/metadata.py` | `ingestion/metadata.py` | 398 | Zero votebot imports — copy as-is |
| `ingestion/chunking.py` | `ingestion/chunking.py` | 316 | Zero votebot imports — copy as-is |
| `ingestion/sources/webflow.py` | `ingestion/sources/webflow.py` | 1,868 | Config, metadata, pipeline, pdf |
| `ingestion/sources/openstates.py` | `ingestion/sources/openstates.py` | 965 | Config, metadata, pipeline |
| `ingestion/sources/pdf.py` | `ingestion/sources/pdf.py` | 272 | Config, metadata, pipeline |
| `ingestion/sources/congress.py` | `ingestion/sources/congress.py` | 300 | Config, metadata, pipeline |

**From `src/votebot/services/` → `src/ddp_sync/services/` (copies):**

| VoteBot path | ddp-sync path | Lines | Notes |
|---|---|---|---|
| `services/embeddings.py` | `services/embeddings.py` | 181 | Config only |
| `services/redis_store.py` | `services/redis_store.py` | 362 | Config only — will remove chat-only methods in Phase 3 |
| `services/vector_store.py` | `services/vector_store.py` | 323 | Config + embeddings |
| `services/webflow_lookup.py` | `services/webflow_lookup.py` | 1,126 | Config only — will remove chat-only methods in Phase 3 |

**From `src/votebot/utils/` → `src/ddp_sync/services/`:**

| VoteBot path | ddp-sync path | Lines | Notes |
|---|---|---|---|
| `utils/legislative_calendar.py` | `services/legislative_calendar.py` | 1,059 | Zero votebot imports — copy as-is |

**Config file:**

| VoteBot path | ddp-sync path | Lines |
|---|---|---|
| `config/sync_schedule.yaml` | `config/sync_schedule.yaml` | 124 |

**Total: ~13,000 lines of code to move.**

## Step 5: Import rewrite

Every moved file needs `votebot.*` imports rewritten to `ddp_sync.*`. The rewrite is mechanical — search-replace with these patterns:

```
from votebot.config import Settings, get_settings
→ from ddp_sync.config import Settings, get_settings

from votebot.config import get_settings
→ from ddp_sync.config import get_settings

from votebot.ingestion.
→ from ddp_sync.ingestion.

from votebot.services.
→ from ddp_sync.services.

from votebot.sync.
→ from ddp_sync.sync.

from votebot.updates.
→ from ddp_sync.pipelines.

from votebot.utils.legislative_calendar
→ from ddp_sync.services.legislative_calendar
```

**Special cases:**

1. **`updates/scheduler.py` → `ddp_sync/scheduler.py`** — imports from `votebot.updates.*` become `ddp_sync.pipelines.*`:
   ```python
   # Before
   from votebot.updates.change_detection import ChangeDetector
   from votebot.updates.bill_version_sync import BillVersionSyncService
   from votebot.updates.bill_sync import BillSyncService
   from votebot.updates.legislator_sync import LegislatorSyncService

   # After
   from ddp_sync.pipelines.change_detection import ChangeDetector
   from ddp_sync.pipelines.bill_version import BillVersionSyncService
   from ddp_sync.pipelines.bill_sync import BillSyncService
   from ddp_sync.pipelines.legislator_sync import LegislatorSyncService
   ```

2. **`sync/handlers/bill.py`** — has both top-level and deferred imports:
   ```python
   # Top-level (line ~6)
   from votebot.updates.bill_sync import BillSyncService
   → from ddp_sync.pipelines.bill_sync import BillSyncService

   # Deferred (line ~448, inside sync_batch)
   from votebot.updates.bill_version_sync import BillVersionSyncService
   → from ddp_sync.pipelines.bill_version import BillVersionSyncService
   ```

3. **`config/sync_schedule.yaml` path** — `updates/scheduler.py` reads this via `DEFAULT_CONFIG_PATH`. Update the path resolution to look relative to the ddp-sync project root, not the votebot project root.

4. **`api/middleware/auth.py`** — the sync routes import `from votebot.api.middleware.auth import api_key_auth`. Create a simple auth module in ddp-sync:

   ```python
   # src/ddp_sync/api/auth.py
   from fastapi import Depends, HTTPException, Security
   from fastapi.security import APIKeyHeader
   from ddp_sync.config import get_settings

   api_key_header = APIKeyHeader(name="Authorization", auto_error=False)

   async def api_key_auth(api_key: str = Security(api_key_header)):
       settings = get_settings()
       expected = f"Bearer {settings.api_key}"
       if not api_key or api_key != expected:
           raise HTTPException(status_code=401, detail="Invalid API key")
       return api_key
   ```

## Step 6: Create AWS Secrets Manager secret

```bash
# Create ddp-sync-secret.json with values from VoteBot .env + DDP-API org-credentials
# (see PLAN-move-sync-to-own-service.md for full template)

aws secretsmanager create-secret \
  --name ddp-sync/credentials \
  --secret-string file://ddp-sync-secret.json \
  --region us-east-1
```

## Step 7: Sync API routes — `src/ddp_sync/api/routes/sync_unified.py`

Copy from `votebot/api/routes/sync_unified.py` (542 lines). Import rewrites:

```python
# Before
from votebot.api.middleware.auth import api_key_auth
from votebot.config import Settings, get_settings
from votebot.services.redis_store import get_redis_store
from votebot.sync import ContentType, SyncIdentifier, SyncMode, SyncOptions, UnifiedSyncService

# After
from ddp_sync.api.auth import api_key_auth
from ddp_sync.config import Settings, get_settings
from ddp_sync.services.redis_store import get_redis_store
from ddp_sync.sync import ContentType, SyncIdentifier, SyncMode, SyncOptions, UnifiedSyncService
```

The route prefix changes from `/sync` to match the ddp-sync API namespace (configured in `app.py`).

## Step 8: Health and schedule routes

```python
# src/ddp_sync/api/routes/health.py
import logging
from fastapi import APIRouter
from ddp_sync.config import get_settings, get_config_source

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/health")
async def health():
    settings = get_settings()
    result = {
        "status": "healthy",
        "service": "ddp-sync",
        "config_source": get_config_source(),
        "redis": "unknown",
        "pinecone": "unknown",
    }

    # Check Redis
    try:
        from ddp_sync.services.redis_store import get_redis_store
        store = get_redis_store()
        if store._redis:
            await store._redis.ping()
            result["redis"] = "connected"
        else:
            result["redis"] = "not_connected"
    except Exception as e:
        result["redis"] = f"error: {e}"

    # Check Pinecone
    try:
        from ddp_sync.services.vector_store import VectorStoreService
        vs = VectorStoreService(settings)
        stats = vs.index.describe_index_stats()
        result["pinecone"] = f"connected ({stats.total_vector_count} vectors)"
    except Exception as e:
        result["pinecone"] = f"error: {e}"

    return result


@router.get("/schedule")
async def schedule():
    """Show all scheduled jobs and their next run times."""
    try:
        from ddp_sync.scheduler import get_scheduler
        scheduler = get_scheduler()
        if not scheduler or not scheduler._scheduler:
            return {"status": "scheduler_not_running", "jobs": []}

        jobs = []
        for job in scheduler._scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time) if job.next_run_time else None,
                "trigger": str(job.trigger),
            })
        return {"status": "running", "jobs": jobs}
    except Exception as e:
        return {"status": f"error: {e}", "jobs": []}
```

## Step 9: FastAPI app — `src/ddp_sync/app.py`

```python
"""DDP-Sync FastAPI application."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ddp_sync.config import get_settings

logger = logging.getLogger(__name__)

API_PREFIX = "/ddp-sync/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: connect Redis, start scheduler. Shutdown: stop scheduler, disconnect."""
    settings = get_settings()

    # Connect Redis
    from ddp_sync.services.redis_store import get_redis_store
    redis_store = get_redis_store()
    await redis_store.connect()

    # Start scheduler (unconditionally — single worker, no leader election)
    from ddp_sync.scheduler import create_scheduler
    scheduler = create_scheduler(settings)
    scheduler.start()
    logger.info("Scheduler started with %d jobs", len(scheduler._scheduler.get_jobs()))

    # Start zombie watchdog (simplified — no leader gating)
    watchdog_task = asyncio.create_task(_zombie_sync_watchdog(redis_store))

    yield

    # Shutdown
    watchdog_task.cancel()
    try:
        await watchdog_task
    except asyncio.CancelledError:
        pass
    scheduler.stop()
    await redis_store.disconnect()
    logger.info("DDP-Sync shutdown complete")


async def _zombie_sync_watchdog(redis_store):
    """Poll for stale sync tasks every 30 minutes."""
    while True:
        try:
            await asyncio.sleep(1800)
            await _check_and_resume_stale_syncs(redis_store)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Watchdog error: {e}")


async def _check_and_resume_stale_syncs(redis_store, stale_threshold=300, max_retries=3):
    """Detect zombie tasks and auto-resume. Simplified from VoteBot's version
    (no leader gating needed — single process)."""
    # Implementation moved from votebot/main.py lines 110-236
    # Key change: uses ddp_sync imports instead of votebot imports
    from ddp_sync.api.routes.sync_unified import _background_tasks, _run_batch_sync_background
    from ddp_sync.sync import ContentType, SyncOptions
    # ... (rest of zombie detection logic, same as VoteBot)
    pass  # TODO: port from votebot/main.py


def create_app() -> FastAPI:
    app = FastAPI(
        title="DDP-Sync",
        description="Unified data pipeline service",
        version="0.1.0",
        lifespan=lifespan,
    )

    from ddp_sync.api.routes.health import router as health_router
    from ddp_sync.api.routes.sync_unified import router as sync_router

    app.include_router(health_router, prefix=API_PREFIX)
    app.include_router(sync_router, prefix=API_PREFIX)

    return app


app = create_app()
```

## Step 10: Scheduler — `src/ddp_sync/scheduler.py`

This is adapted from VoteBot's `updates/scheduler.py` (828 lines). Key changes:
- No leader election (single worker)
- Uses `ddp_sync.config.get_settings()` instead of `votebot.config.get_settings()`
- Reads `config/sync_schedule.yaml` relative to ddp-sync project root
- Phase 1: only 3 VoteBot jobs. Phase 2 adds 8 DDP-API jobs.

```python
"""Unified scheduler for DDP-Sync.

Phase 1: VoteBot sync jobs (bill version, legislator, org).
Phase 2: DDP-API jobs (Voatz→Brevo, Webflow batch).
"""

import logging
import os
from pathlib import Path

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ddp_sync.config import SyncSettings, get_settings

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "sync_schedule.yaml"

_scheduler_instance = None


class SyncScheduler:
    def __init__(self, settings: SyncSettings, config_path: str | None = None):
        self.settings = settings
        self._scheduler = AsyncIOScheduler()
        self._config = self._load_config(config_path or str(DEFAULT_CONFIG_PATH))
        self._register_jobs()

    def _load_config(self, path: str) -> dict:
        if os.path.exists(path):
            with open(path) as f:
                return yaml.safe_load(f)
        logger.warning(f"Config file not found: {path}, using defaults")
        return {}

    def _register_jobs(self):
        """Register Phase 1 sync jobs."""
        sync_config = self._config.get("sync_time_utc", {})

        # Bill version sync — daily at 04:00 UTC
        self._scheduler.add_job(
            self._run_bill_version_sync,
            CronTrigger(hour=sync_config.get("hour", 4), minute=sync_config.get("minute", 0)),
            id="bill_version_sync",
            name="Bill version sync (OpenStates → Webflow + Pinecone)",
        )

        # Legislator sync — weekly Sunday at 06:00 UTC
        leg_config = self._config.get("legislator_sync", {})
        self._scheduler.add_job(
            self._run_legislator_sync,
            CronTrigger(
                day_of_week=leg_config.get("day_of_week", "sun"),
                hour=leg_config.get("hour", 6),
                minute=leg_config.get("minute", 0),
            ),
            id="legislator_sync",
            name="Legislator sync (OpenStates → Pinecone)",
        )

        # Organization sync — monthly 1st at 08:00 UTC
        org_config = self._config.get("organization_sync", {})
        self._scheduler.add_job(
            self._run_organization_sync,
            CronTrigger(
                day=org_config.get("day", 1),
                hour=org_config.get("hour", 8),
                minute=org_config.get("minute", 0),
            ),
            id="organization_sync",
            name="Organization sync (Webflow → Pinecone)",
        )

    async def _run_bill_version_sync(self):
        """Daily bill version check + re-ingestion."""
        logger.info("Starting scheduled bill version sync")
        try:
            from ddp_sync.pipelines.bill_version import BillVersionSyncService
            service = BillVersionSyncService(self.settings)
            result = await service.sync_bill_versions()
            logger.info(f"Bill version sync complete: {result}")
        except Exception as e:
            logger.error(f"Bill version sync failed: {e}", exc_info=True)

    async def _run_legislator_sync(self):
        """Weekly legislator profile + voting records sync."""
        logger.info("Starting scheduled legislator sync")
        try:
            from ddp_sync.pipelines.legislator_sync import LegislatorSyncService
            from ddp_sync.ingestion.pipeline import IngestionPipeline
            from ddp_sync.ingestion.sources.webflow import WebflowSource
            from ddp_sync.ingestion.metadata import MetadataExtractor

            pipeline = IngestionPipeline(self.settings)
            source = WebflowSource(self.settings)
            service = LegislatorSyncService(self.settings)
            # ... sync logic adapted from votebot/updates/scheduler.py _run_legislator_bills_sync()
            logger.info("Legislator sync complete")
        except Exception as e:
            logger.error(f"Legislator sync failed: {e}", exc_info=True)

    async def _run_organization_sync(self):
        """Monthly organization data sync."""
        logger.info("Starting scheduled organization sync")
        try:
            from ddp_sync.sync.handlers.organization import OrganizationHandler
            from ddp_sync.sync.types import SyncOptions
            handler = OrganizationHandler(self.settings)
            options = SyncOptions()
            result = await handler.sync_batch(options)
            logger.info(f"Organization sync complete: {result}")
        except Exception as e:
            logger.error(f"Organization sync failed: {e}", exc_info=True)

    def start(self):
        self._scheduler.start()

    def stop(self):
        self._scheduler.shutdown(wait=False)


def create_scheduler(settings: SyncSettings | None = None) -> SyncScheduler:
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = SyncScheduler(settings or get_settings())
    return _scheduler_instance


def get_scheduler() -> SyncScheduler | None:
    return _scheduler_instance
```

**Key difference from VoteBot's scheduler:** Uses `AsyncIOScheduler` (not `BackgroundScheduler`) since ddp-sync is a single-worker async service. This integrates cleanly with the FastAPI event loop.

## Step 11: `.env.example`

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

## Step 12: systemd service file

```ini
# infrastructure/ddp-sync.service
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

---

## Verification checklist

- [x] `ddp-sync` repo created on GitHub (`Digital-Democracy-Project/ddp-sync`)
- [x] All files copied and imports rewritten (`votebot.*` → `ddp_sync.*`) — 42 Python files, 0 remaining `votebot` imports
- [x] `pip install -e .` succeeds in a fresh venv
- [x] `python -c "from ddp_sync.config import get_settings; ..."` works with `.env` (config source: env)
- [x] App creation succeeds — all 5 routes registered under `/ddp-sync/v1/`
- [ ] `uvicorn ddp_sync.app:app --port 8001` starts without errors (requires Redis)
- [ ] `curl localhost:8001/ddp-sync/v1/health` returns config_source, redis, pinecone status (requires Redis)
- [ ] `curl localhost:8001/ddp-sync/v1/schedule` shows 3 jobs (requires Redis)
- [ ] Single-bill sync via API returns task_id and completes successfully (requires Redis + credentials)
- [x] AWS Secrets Manager secret `ddp-sync/credentials` created and populated

### Fixes applied during verification

1. **`pyproject.toml` build-backend**: Changed from `setuptools.backends._legacy:_Backend` to `setuptools.build_meta`
2. **`pinecone-client` → `pinecone`**: Package was renamed; updated dependency
3. **Missing dependencies**: Added `tenacity>=8.0.0` and `PyPDF2>=3.0.0`
4. **`UpdateSchedulerFactory.create()`**: Method doesn't exist; fixed to `.get_instance()`
5. **`scheduler._scheduler`**: Attribute is `scheduler.scheduler` (public); fixed in `health.py`
6. **`get_scheduler()` missing**: Added helper function to `scheduler.py` for health endpoint

---

## Files created (this phase)

| File | Lines (est.) | Source |
|------|---|---|
| `pyproject.toml` | ~50 | New |
| `src/ddp_sync/config.py` | ~120 | New (replaces pydantic Settings) |
| `src/ddp_sync/app.py` | ~100 | New (simplified from votebot/main.py) |
| `src/ddp_sync/scheduler.py` | ~150 | Adapted from votebot/updates/scheduler.py |
| `src/ddp_sync/api/auth.py` | ~20 | New (simplified auth) |
| `src/ddp_sync/api/routes/health.py` | ~60 | New |
| `src/ddp_sync/api/routes/sync_unified.py` | ~542 | Copied from votebot, imports rewritten |
| `src/ddp_sync/sync/**` | ~3,515 | Copied from votebot/sync/, imports rewritten |
| `src/ddp_sync/pipelines/**` | ~4,206 | Copied from votebot/updates/, imports rewritten |
| `src/ddp_sync/ingestion/**` | ~4,538 | Copied from votebot/ingestion/, imports rewritten |
| `src/ddp_sync/services/**` | ~3,051 | Copied from votebot/services/ + utils/, imports rewritten |
| `config/sync_schedule.yaml` | 124 | Copied from votebot |
| `infrastructure/ddp-sync.service` | 15 | New |
| `.env.example` | 22 | New |
