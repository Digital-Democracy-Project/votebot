"""Content type handlers for the unified sync service."""

from votebot.sync.handlers.base import ContentHandler
from votebot.sync.handlers.bill import BillHandler
from votebot.sync.handlers.legislator import LegislatorHandler
from votebot.sync.handlers.organization import OrganizationHandler
from votebot.sync.handlers.training import TrainingHandler
from votebot.sync.handlers.webpage import WebpageHandler

__all__ = [
    "ContentHandler",
    "BillHandler",
    "LegislatorHandler",
    "OrganizationHandler",
    "TrainingHandler",
    "WebpageHandler",
]
