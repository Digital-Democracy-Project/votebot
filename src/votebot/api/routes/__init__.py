"""API routes for VoteBot."""

from votebot.api.routes.chat import router as chat_router
from votebot.api.routes.content import router as content_router
from votebot.api.routes.health import router as health_router
from votebot.api.routes.sync import router as sync_router
from votebot.api.routes.sync_unified import router as sync_unified_router
from votebot.api.routes.websocket import router as websocket_router

__all__ = [
    "chat_router",
    "content_router",
    "health_router",
    "sync_router",
    "sync_unified_router",
    "websocket_router",
]
