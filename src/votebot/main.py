"""FastAPI application entry point for VoteBot."""

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import structlog
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from votebot.api.middleware.logging import LoggingMiddleware
from votebot.api.routes import (
    chat_router,
    content_router,
    health_router,
    sync_router,
    sync_unified_router,
    websocket_router,
)
from votebot.api.schemas.common import ErrorResponse
from votebot.config import get_settings
from votebot.utils.logging import setup_logging

# Clear settings cache to ensure fresh env vars on reload
get_settings.cache_clear()
settings = get_settings()
logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager for startup and shutdown events."""
    # Startup
    setup_logging(settings.log_level)

    # Initialize Redis for cross-worker shared state
    from votebot.services.redis_store import get_redis_store
    redis_store = get_redis_store()
    await redis_store.connect()

    logger.info(
        "Starting VoteBot API",
        version=settings.app_version,
        environment=settings.environment,
    )

    # Start scheduler if enabled (with leader election for multi-worker safety)
    scheduler_worker_id = str(uuid.uuid4())[:8]
    # Mutable state dict so inner async functions can share/modify state
    state = {"scheduler": None, "refresh_task": None, "bg_task": None, "watchdog_task": None}

    if settings.scheduler_enabled:

        async def _start_as_leader():
            """Promote this worker to scheduler leader."""
            from votebot.updates.scheduler import UpdateSchedulerFactory
            state["scheduler"] = UpdateSchedulerFactory.get_instance(settings)
            state["scheduler"].start()
            state["refresh_task"] = asyncio.create_task(_refresh_lock())
            state["watchdog_task"] = asyncio.create_task(_zombie_sync_watchdog())

        async def _refresh_lock():
            """Refresh leader lock every 2 min. Stop scheduler if lock lost."""
            while True:
                await asyncio.sleep(120)  # Refresh every 2 minutes (TTL is 5 min)
                ok = await redis_store.refresh_scheduler_lock(scheduler_worker_id)
                if not ok:
                    logger.warning("Lost scheduler leader lock, stopping scheduler")
                    if state["scheduler"] and state["scheduler"].is_running:
                        state["scheduler"].stop()
                    state["scheduler"] = None
                    break

        async def _try_become_leader():
            """Follower re-election loop: attempt to acquire leader lock every 60s."""
            while True:
                await asyncio.sleep(60)
                acquired = await redis_store.acquire_scheduler_lock(scheduler_worker_id)
                if acquired:
                    logger.info(
                        "Promoted to scheduler leader via re-election",
                        worker_id=scheduler_worker_id,
                    )
                    await _start_as_leader()
                    break

        async def _zombie_sync_watchdog():
            """
            Poll Redis every 30 minutes for zombie sync tasks and auto-resume them.

            A task is a zombie if status == "running" and last_heartbeat is >5 minutes old.
            Runs on the leader worker only.
            """
            POLL_INTERVAL = 1800  # 30 minutes
            STALE_THRESHOLD = timedelta(minutes=5)
            MAX_RETRIES = 3

            while True:
                try:
                    await _check_and_resume_stale_syncs(STALE_THRESHOLD, MAX_RETRIES)
                except Exception as e:
                    logger.exception("Zombie sync watchdog error", error=str(e))
                await asyncio.sleep(POLL_INTERVAL)

        async def _check_and_resume_stale_syncs(
            stale_threshold: timedelta,
            max_retries: int,
        ):
            """Scan Redis for zombie sync tasks and auto-resume them."""
            from votebot.api.routes.sync_unified import (
                _background_tasks,
                _run_batch_sync_background,
            )
            from votebot.sync import ContentType, SyncOptions

            if not redis_store or not redis_store._client:
                return

            # Scan for sync task keys
            keys = []
            async for key in redis_store._client.scan_iter(match="votebot:sync:task:*"):
                keys.append(key)

            for key in keys:
                task_data = await redis_store._client.get(key)
                if not task_data:
                    continue

                task = json.loads(task_data)

                if task.get("status") != "running":
                    continue

                # Check if heartbeat is stale
                heartbeat = task.get("last_heartbeat")
                if not heartbeat:
                    continue

                heartbeat_time = datetime.fromisoformat(heartbeat)
                if heartbeat_time.tzinfo is None:
                    heartbeat_time = heartbeat_time.replace(tzinfo=timezone.utc)
                cutoff = datetime.now(timezone.utc) - stale_threshold

                if heartbeat_time > cutoff:
                    continue  # Still fresh, another worker might be running it

                # Extract task_id from key
                key_str = key.decode() if isinstance(key, bytes) else key
                old_task_id = key_str.split(":")[-1]

                # Check retry limit
                retry_count = task.get("retry_count", 0)
                if retry_count >= max_retries:
                    logger.error(
                        "Sync task exceeded max retries, marking as permanently failed",
                        task_id=old_task_id,
                        retry_count=retry_count,
                        content_type=task.get("content_type"),
                        items_processed=task.get("result", {}).get("items_processed", 0),
                    )
                    task["status"] = "permanently_failed"
                    task["error"] = (
                        f"Exceeded max retries ({max_retries}). "
                        f"Worker crashed {retry_count} times. "
                        f"Last heartbeat: {heartbeat}. "
                        f"Manual intervention required."
                    )
                    await redis_store.set_sync_task(old_task_id, task)
                    continue

                # This is a zombie task — mark old task as failed and resume
                content_type_str = task.get("content_type", "bill")
                saved_options = task.get("options", {})

                logger.warning(
                    "Found stale sync task from crashed worker, auto-resuming",
                    old_task_id=old_task_id,
                    content_type=content_type_str,
                    retry_count=retry_count,
                    items_processed=task.get("result", {}).get("items_processed", 0),
                    last_heartbeat=heartbeat,
                )

                task["status"] = "failed"
                task["error"] = (
                    f"Worker died (OOM or crash). "
                    f"Auto-resuming as retry {retry_count + 1}/{max_retries}."
                )
                await redis_store.set_sync_task(old_task_id, task)

                # Start a new sync with resume
                new_task_id = str(uuid.uuid4())
                options = SyncOptions(
                    include_pdfs=saved_options.get("include_pdfs", True),
                    include_openstates=saved_options.get("include_openstates", True),
                    include_sponsored_bills=saved_options.get("include_sponsored_bills", True),
                    jurisdiction=saved_options.get("jurisdiction"),
                    limit=saved_options.get("limit", 0),
                    dry_run=saved_options.get("dry_run", False),
                    resume_task_id=old_task_id,
                )

                # Copy checkpoints from old task
                copied = await redis_store.copy_sync_checkpoints(old_task_id, new_task_id)

                # Register new task in background_tasks dict
                _background_tasks[new_task_id] = {
                    "status": "accepted",
                    "content_type": content_type_str,
                    "mode": "batch",
                    "started_at": time.time(),
                    "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                    "retry_count": retry_count + 1,
                    "resumed_from": old_task_id,
                    "options": saved_options,
                }
                await redis_store.set_sync_task(new_task_id, _background_tasks[new_task_id])

                # Start the background sync
                content_type_enum = ContentType(content_type_str)
                asyncio.create_task(
                    _run_batch_sync_background(new_task_id, content_type_enum, options, settings)
                )

                logger.info(
                    "Auto-resumed sync task",
                    new_task_id=new_task_id,
                    old_task_id=old_task_id,
                    retry_count=retry_count + 1,
                    checkpoints_copied=copied,
                )

        is_leader = await redis_store.acquire_scheduler_lock(scheduler_worker_id)
        if is_leader:
            await _start_as_leader()
            logger.info(
                "Scheduler started (this worker is leader)",
                worker_id=scheduler_worker_id,
            )
            # bg_task tracks refresh_task for shutdown; already set by _start_as_leader
        else:
            logger.info(
                "Scheduler not started (another worker is leader), "
                "will re-check every 60s",
                worker_id=scheduler_worker_id,
            )
            state["bg_task"] = asyncio.create_task(_try_become_leader())

    yield

    # Shutdown — cancel whichever background tasks are active
    for key in ("bg_task", "refresh_task", "watchdog_task"):
        task = state.get(key)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    if state["scheduler"] and state["scheduler"].is_running:
        state["scheduler"].stop()
        await redis_store.release_scheduler_lock(scheduler_worker_id)

    await redis_store.disconnect()
    logger.info("Shutting down VoteBot API")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title=settings.app_name,
        description="High-performance, context-aware chat API for Digital Democracy Project",
        version=settings.app_version,
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
        openapi_url="/openapi.json" if settings.debug else None,
        lifespan=lifespan,
    )

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Add custom logging middleware
    app.add_middleware(LoggingMiddleware)

    # Include routers
    app.include_router(health_router, prefix=settings.api_prefix)
    app.include_router(chat_router, prefix=settings.api_prefix)
    app.include_router(content_router, prefix=settings.api_prefix)
    app.include_router(sync_router, prefix=settings.api_prefix)
    app.include_router(sync_unified_router, prefix=settings.api_prefix)
    app.include_router(websocket_router)  # WebSocket at root level

    # Global exception handlers
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Handle uncaught exceptions."""
        logger.exception(
            "Unhandled exception",
            path=request.url.path,
            method=request.method,
            error=str(exc),
        )
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="internal_error",
                message="An unexpected error occurred",
                detail=str(exc) if settings.debug else None,
            ).model_dump(mode="json"),
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        """Handle validation errors."""
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                error="validation_error",
                message=str(exc),
            ).model_dump(mode="json"),
        )

    return app


# Create the app instance
app = create_app()


def run() -> None:
    """Run the application with Uvicorn."""
    uvicorn.run(
        "votebot.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()
