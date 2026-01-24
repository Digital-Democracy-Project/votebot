"""API routes for VoteBot."""

from votebot.api.routes.chat import router as chat_router
from votebot.api.routes.health import router as health_router

__all__ = ["chat_router", "health_router"]
