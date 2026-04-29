"""Admin endpoints for the button cache.

Authenticated via the standard Bearer token (same as the chat endpoint).
Used as a runbook tool for force-clearing a bill's cached responses when
the normal pub/sub invalidation isn't sufficient (e.g., manual content
edit that bypasses ddp-sync's bill-version detection).
"""

from fastapi import APIRouter, Depends, HTTPException, status

from votebot.api.middleware.auth import bearer_auth
from votebot.services.button_cache import get_button_cache

router = APIRouter()


@router.delete(
    "/cache/button/{slug}",
    dependencies=[Depends(bearer_auth)],
    status_code=status.HTTP_200_OK,
)
async def invalidate_button_cache(slug: str) -> dict:
    """Force-clear all cached button responses for a bill slug.

    Returns the number of cache entries deleted (0..2 — one per cacheable
    button type). Idempotent: deleting a non-existent key is a no-op.
    """
    if not slug:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="slug is required",
        )
    cache = get_button_cache()
    deleted = await cache.invalidate_bill(slug)
    return {"slug": slug, "deleted": deleted}
