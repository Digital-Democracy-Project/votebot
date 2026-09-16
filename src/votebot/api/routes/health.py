"""Health check endpoints."""

import httpx
import structlog
from fastapi import APIRouter, Depends

from votebot.api.schemas.common import HealthResponse
from votebot.config import Settings, get_settings
from votebot.services.openstates_client import openstates_base_url

router = APIRouter(tags=["health"])
logger = structlog.get_logger()


@router.get("/health", response_model=HealthResponse)
async def health_check(settings: Settings = Depends(get_settings)) -> HealthResponse:
    """Basic health check endpoint."""
    return HealthResponse(
        status="healthy",
        version=settings.app_version,
        environment=settings.environment,
        dependencies={},
    )


@router.get("/health/ready", response_model=HealthResponse)
async def readiness_check(settings: Settings = Depends(get_settings)) -> HealthResponse:
    """
    Readiness check that verifies all dependencies are available.

    This endpoint checks connectivity to:
    - Pinecone vector store
    - OpenAI API
    - Redis cache (if configured)
    """
    dependencies: dict[str, str] = {}
    overall_status = "healthy"

    # Check Pinecone connectivity
    try:
        # Import here to avoid circular imports
        from votebot.services.vector_store import VectorStoreService

        vector_store = VectorStoreService()
        await vector_store.health_check()
        dependencies["pinecone"] = "healthy"
    except Exception as e:
        logger.warning("Pinecone health check failed", error=str(e))
        dependencies["pinecone"] = "unhealthy"
        overall_status = "degraded"

    # Check OpenAI connectivity
    try:
        from votebot.services.llm import LLMService

        llm = LLMService()
        await llm.health_check()
        dependencies["openai"] = "healthy"
    except Exception as e:
        logger.warning("OpenAI health check failed", error=str(e))
        dependencies["openai"] = "unhealthy"
        overall_status = "degraded"

    # Check DDP OpenStates replica connectivity (via ddp-api's unauthenticated
    # /openstates/healthz passthrough) -- only meaningful once VOTEBOT-2's
    # replica routing is enabled; skipped entirely on the public-API path.
    if settings.use_ddp_openstates_replica:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{openstates_base_url(settings)}/healthz")
                response.raise_for_status()
            dependencies["ddp_openstates_replica"] = "healthy"
        except Exception as e:
            logger.warning("DDP OpenStates replica health check failed", error=str(e))
            dependencies["ddp_openstates_replica"] = "unhealthy"
            overall_status = "degraded"

    # Check Redis connectivity (optional)
    try:
        import redis.asyncio as redis

        client = redis.from_url(settings.redis_url)
        await client.ping()
        await client.close()
        dependencies["redis"] = "healthy"
    except Exception as e:
        logger.debug("Redis health check failed (optional)", error=str(e))
        dependencies["redis"] = "unavailable"

    return HealthResponse(
        status=overall_status,
        version=settings.app_version,
        environment=settings.environment,
        dependencies=dependencies,
    )


@router.get("/health/live")
async def liveness_check() -> dict[str, str]:
    """
    Liveness check for Kubernetes/ECS health probes.

    Returns a simple response to indicate the service is running.
    """
    return {"status": "alive"}
