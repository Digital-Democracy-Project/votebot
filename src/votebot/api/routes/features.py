"""Feature-flag discovery endpoint.

Used by the chat widget to learn which features are enabled in this
deployment. Public (unauthenticated) — only exposes booleans, no secrets.
"""

from fastapi import APIRouter

from votebot.config import get_settings

router = APIRouter()


@router.get("/features")
async def get_features() -> dict:
    """Return the set of enabled feature flags relevant to the chat UI."""
    settings = get_settings()
    return {
        "quick_action_buttons_enabled": getattr(
            settings, "quick_action_buttons_enabled", False
        ),
    }
