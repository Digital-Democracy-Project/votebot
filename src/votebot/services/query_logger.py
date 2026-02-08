"""Production query logger — captures all user queries and LLM responses to JSONL files."""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiofiles
import structlog

from votebot.config import get_settings

logger = structlog.get_logger()

# Module-level singleton
_query_logger: "QueryLogger | None" = None


class QueryLogger:
    """Async JSONL logger for production query monitoring.

    Writes date-partitioned JSONL files (e.g., logs/queries/2026-02-08.jsonl).
    Uses aiofiles with O_APPEND for atomic multi-worker writes.
    """

    def __init__(self, log_dir: str):
        self.log_dir = Path(log_dir)
        self._ensured_dirs: set[str] = set()

    def _ensure_dir(self) -> None:
        """Create log directory if it doesn't exist (cached)."""
        dir_str = str(self.log_dir)
        if dir_str not in self._ensured_dirs:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self._ensured_dirs.add(dir_str)

    def _log_path(self, date: datetime) -> Path:
        """Get the JSONL file path for a given date."""
        return self.log_dir / f"{date.strftime('%Y-%m-%d')}.jsonl"

    async def log_query(
        self,
        *,
        session_id: str,
        message: str,
        response: str,
        confidence: float,
        citations: list[dict[str, Any]],
        page_context: dict[str, Any],
        channel: str,
        duration_ms: int,
        human_active: bool = False,
    ) -> None:
        """Append a query log entry to the date-partitioned JSONL file.

        Args:
            session_id: Chat session identifier.
            message: User's query text.
            response: LLM response text.
            confidence: Response confidence score.
            citations: List of citation dicts.
            page_context: Page context dict.
            channel: "rest" or "websocket".
            duration_ms: Response time in milliseconds.
            human_active: Whether a human agent was active.
        """
        now = datetime.now(timezone.utc)
        entry = {
            "timestamp": now.isoformat(),
            "session_id": session_id,
            "message": message,
            "response": response,
            "confidence": confidence,
            "citations": citations,
            "page_context": page_context,
            "channel": channel,
            "duration_ms": duration_ms,
            "human_active": human_active,
        }

        try:
            self._ensure_dir()
            log_file = self._log_path(now)
            line = json.dumps(entry, default=str) + "\n"
            async with aiofiles.open(log_file, mode="a") as f:
                await f.write(line)
        except Exception:
            logger.warning("Failed to write query log entry", exc_info=True)


def get_query_logger() -> QueryLogger | None:
    """Get the module-level QueryLogger singleton.

    Returns None if query logging is disabled in settings.
    """
    global _query_logger
    settings = get_settings()
    if not settings.query_log_enabled:
        return None
    if _query_logger is None:
        _query_logger = QueryLogger(log_dir=settings.query_log_dir)
    return _query_logger
