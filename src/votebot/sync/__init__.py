"""Unified sync service for VoteBot content ingestion."""

from votebot.sync.handlers import (
    BillHandler,
    ContentHandler,
    LegislatorHandler,
    OrganizationHandler,
    TrainingHandler,
    WebpageHandler,
)
from votebot.sync.service import UnifiedSyncService
from votebot.sync.types import ContentType, SyncIdentifier, SyncMode, SyncOptions, SyncResult

__all__ = [
    # Service
    "UnifiedSyncService",
    # Types
    "ContentType",
    "SyncMode",
    "SyncIdentifier",
    "SyncOptions",
    "SyncResult",
    # Handlers
    "ContentHandler",
    "BillHandler",
    "LegislatorHandler",
    "OrganizationHandler",
    "WebpageHandler",
    "TrainingHandler",
]
