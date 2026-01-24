"""Real-time update handling for VoteBot."""

from votebot.updates.change_detection import ChangeDetector
from votebot.updates.scheduler import UpdateScheduler

__all__ = ["UpdateScheduler", "ChangeDetector"]
