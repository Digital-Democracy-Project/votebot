"""FastAPI application entry point for VoteBot."""

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

    # Initialize services (lazy initialization is also supported)
    yield

    # Shutdown
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
