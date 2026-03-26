"""Production query and event logger — captures queries, events, and conversation summaries to JSONL."""

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


def _derive_device_type(user_agent: str | None) -> str:
    """Derive device type from User-Agent string."""
    if not user_agent:
        return "unknown"
    ua = user_agent.lower()
    if "ipad" in ua or "tablet" in ua:
        return "tablet"
    if "mobile" in ua or "iphone" in ua or "android" in ua:
        return "mobile"
    return "desktop"


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

    async def _write_entry(self, entry: dict[str, Any]) -> None:
        """Write a single entry to the JSONL file."""
        try:
            self._ensure_dir()
            now = datetime.now(timezone.utc)
            log_file = self._log_path(now)
            # Strip None values to keep entries compact
            clean = {k: v for k, v in entry.items() if v is not None}
            line = json.dumps(clean, default=str) + "\n"
            async with aiofiles.open(log_file, mode="a") as f:
                await f.write(line)
        except Exception:
            logger.warning("Failed to write log entry", exc_info=True)

    async def log_event(
        self,
        *,
        event_type: str,
        # Identity
        visitor_id: str | None = None,
        session_id: str,
        conversation_id: str | None = None,
        session_message_index: int | None = None,
        conversation_message_index: int | None = None,
        # Content (optional, not present on all event types)
        message: str | None = None,
        response: str | None = None,
        # Behavioral
        primary_intent: str | None = None,
        sub_intent: str | None = None,
        confidence: float | None = None,
        retrieval_count: int | None = None,
        retrieval_sources: list[str] | None = None,
        has_citations: bool | None = None,
        citations_count: int | None = None,
        grounding_status: str | None = None,
        external_augmentation: str | None = None,
        web_search_used: bool = False,
        fallback_used: bool = False,
        fallback_reason: str | None = None,
        bill_votes_tool_used: bool = False,
        handoff_triggered: bool = False,
        error: bool = False,
        error_type: str | None = None,
        # Conversation summary (for conversation_ended)
        turn_count: int | None = None,
        duration_seconds: int | None = None,
        handoff_occurred: bool | None = None,
        fallback_occurred: bool | None = None,
        retrieval_miss_occurred: bool | None = None,
        terminal_state: str | None = None,
        primary_intents_seen: list[str] | None = None,
        dominant_primary_intent: str | None = None,
        # Context
        page_context: dict[str, Any] | None = None,
        device_type: str | None = None,
        entry_referrer: str | None = None,
        page_url: str | None = None,
        scroll_depth: float | None = None,
        time_on_page: int | None = None,
        channel: str | None = None,
        duration_ms: int | None = None,
        citations: list[dict[str, Any]] | None = None,
        human_active: bool = False,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Write a structured event to the JSONL log.

        All events share the same file. The event_type field distinguishes them.
        """
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            # Identity
            "visitor_id": visitor_id,
            "session_id": session_id,
            "conversation_id": conversation_id,
            "session_message_index": session_message_index,
            "conversation_message_index": conversation_message_index,
            # Content
            "message": message,
            "response": response,
            # Behavioral
            "primary_intent": primary_intent,
            "sub_intent": sub_intent,
            "confidence": confidence,
            "retrieval_count": retrieval_count,
            "retrieval_sources": retrieval_sources,
            "has_citations": has_citations,
            "citations_count": citations_count,
            "grounding_status": grounding_status,
            "external_augmentation": external_augmentation,
            "web_search_used": web_search_used if web_search_used else None,
            "fallback_used": fallback_used if fallback_used else None,
            "fallback_reason": fallback_reason,
            "bill_votes_tool_used": bill_votes_tool_used if bill_votes_tool_used else None,
            "handoff_triggered": handoff_triggered if handoff_triggered else None,
            "error": error if error else None,
            "error_type": error_type,
            # Conversation summary
            "turn_count": turn_count,
            "duration_seconds": duration_seconds,
            "handoff_occurred": handoff_occurred,
            "fallback_occurred": fallback_occurred,
            "retrieval_miss_occurred": retrieval_miss_occurred,
            "terminal_state": terminal_state,
            "primary_intents_seen": primary_intents_seen,
            "dominant_primary_intent": dominant_primary_intent,
            # Context
            "page_context": page_context,
            "device_type": device_type or _derive_device_type(user_agent),
            "entry_referrer": entry_referrer,
            "page_url": page_url,
            "scroll_depth": scroll_depth,
            "time_on_page": time_on_page,
            "channel": channel,
            "duration_ms": duration_ms,
            "citations": citations,
            "human_active": human_active if human_active else None,
            "client_ip": client_ip,
            "user_agent": user_agent,
        }
        await self._write_entry(entry)

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
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Backward-compatible wrapper — writes a legacy-format query log entry.

        New code should use log_event() instead.
        """
        now = datetime.now(timezone.utc)
        entry = {
            "timestamp": now.isoformat(),
            "session_id": session_id,
            "client_ip": client_ip,
            "user_agent": user_agent,
            "message": message,
            "response": response,
            "confidence": confidence,
            "citations": citations,
            "page_context": page_context,
            "channel": channel,
            "duration_ms": duration_ms,
            "human_active": human_active,
        }
        await self._write_entry(entry)


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
