"""FastAPI application entry point for VoteBot."""

import asyncio
import uuid

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
    state = {"scheduler": None, "refresh_task": None, "bg_task": None}

    if settings.scheduler_enabled:

        async def _start_as_leader():
            """Promote this worker to scheduler leader."""
            from votebot.updates.scheduler import UpdateSchedulerFactory
            state["scheduler"] = UpdateSchedulerFactory.get_instance(settings)
            state["scheduler"].start()
            state["refresh_task"] = asyncio.create_task(_refresh_lock())

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
    for key in ("bg_task", "refresh_task"):
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
