"""End-to-end test for the streaming cache-hit branch of process_message_stream.

Plan §1.5 — the cache-hit hang of commit 88b9dd2 was a streaming-specific bug
(text + done emitted as a single chunk, dropped by the WS handler). This test
locks in three properties at once:

1. The JSONL ``query_processed`` event preserves the cached grounding triplet
   (grounding_status, retrieval_count, retrieval_sources) instead of defaulting
   to ``ungrounded`` from ``retrieval_count=0`` on the replay path. This is
   the B1+B2 fix from plan §1.3.

2. ``cache_hit=True`` and ``button_type=...`` are populated on the event.

3. Stream chunks emit in ``text-first, done-second`` order so the WS handler
   sees a real ``stream_chunk`` before the ``stream_end``. This prevents a
   regression of the 88b9dd2 hang.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from votebot.api.schemas.chat import PageContext
from votebot.core.agent import VoteBotAgent
from votebot.services.button_cache import ButtonCache, make_key


class FakeRedisClient:
    """Minimal in-memory async redis stand-in (mirrors test_button_cache.py)."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.store[key] = value
        return True

    async def delete(self, key: str):
        return 1 if self.store.pop(key, None) is not None else 0


@pytest.fixture
def cached_v2_payload():
    """The cached payload mimics what _populate_button_cache writes on a miss."""
    return {
        "response": "## Summary of HR 1234\n\nThis bill establishes...",
        "citations": [
            {
                "source": "Congress.gov",
                "document_id": "bill-HR-1234",
                "excerpt": "The bill text...",
                "url": "https://congress.gov/bill/HR-1234",
                "relevance_score": 0.92,
            }
        ],
        "confidence": 0.88,
        "grounding_status": "grounded",
        "retrieval_count": 4,
        "retrieval_sources": ["bill", "bill-webflow"],
        "cached_at": "2026-04-30T12:00:00+00:00",
        "button_type": "summary",
    }


@pytest.fixture
def primed_button_cache(cached_v2_payload, monkeypatch):
    """Patch ``get_button_cache`` to return a ButtonCache backed by an
    in-memory fake redis pre-loaded with a v2 entry for hr-1234-2025/summary.
    """
    rs = MagicMock()
    rs._client = FakeRedisClient()
    cache = ButtonCache(rs)

    # Pre-seed the v2 entry under the canonical key shape.
    rs._client.store[make_key("hr-1234-2025", "summary")] = json.dumps(
        cached_v2_payload
    )

    monkeypatch.setattr(
        "votebot.services.button_cache.get_button_cache", lambda: cache
    )
    # The agent imports get_button_cache lazily inside _maybe_serve_from_button_cache,
    # so the module-level patch above is sufficient.
    return cache


@pytest.fixture
def captured_log_events(monkeypatch):
    """Replace ``get_query_logger`` with a stub that captures log_event calls."""
    captured: list[dict] = []

    class StubLogger:
        async def log_event(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr(
        "votebot.services.query_logger.get_query_logger", lambda: StubLogger()
    )
    return captured


@pytest.fixture
def agent_with_buttons_enabled(settings):
    """An agent instance with quick_action_buttons_enabled=True so the
    cache-hit branch is reachable. Other dependencies are lightly stubbed
    because the cache-hit branch returns before invoking them.
    """
    settings.quick_action_buttons_enabled = True
    agent = VoteBotAgent(settings=settings)
    # The cache-hit path doesn't touch retrieval/llm/webflow — but the
    # constructor instantiates them. Replacing with MagicMock avoids real
    # network calls if anything in the hit path expanded later.
    agent.retrieval = MagicMock()
    agent.llm = MagicMock()
    agent.web_search = MagicMock()
    agent.webflow_lookup = MagicMock()
    return agent


@pytest.mark.asyncio
async def test_streaming_cache_hit_preserves_grounding_metadata(
    agent_with_buttons_enabled,
    primed_button_cache,
    captured_log_events,
    cached_v2_payload,
):
    """The end-to-end cache-hit replay path must:

    - Emit chunks in text-first, done-second order.
    - Log a ``query_processed`` event with the cached grounding triplet
      and ``cache_hit=True`` + ``button_type="summary"``.
    """
    page_context = PageContext(
        type="bill",
        id="HR-1234",
        jurisdiction="US",
        title="Sample Bill Act",
        slug="hr-1234-2025",
    )

    chunks = []
    async for chunk in agent_with_buttons_enabled.process_message_stream(
        message="Summarize this bill",
        session_id="sess-test-123",
        page_context=page_context,
        button="summary",
        visitor_id="v_test",
        conversation_id="conv-1",
        session_message_index=1,
        conversation_message_index=1,
    ):
        chunks.append(chunk)

    # Chunk ordering: at minimum a text chunk (done=False) before a done chunk.
    # This is the contract the WS handler depends on (commit 88b9dd2).
    assert len(chunks) >= 2
    assert chunks[0].done is False
    assert chunks[0].text == cached_v2_payload["response"]
    assert chunks[-1].done is True

    # _log_query is fire-and-forget via asyncio.create_task; give it a tick.
    import asyncio
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Exactly one query_processed event for the cache-hit replay.
    qp_events = [e for e in captured_log_events if e.get("event_type") == "query_processed"]
    assert len(qp_events) == 1, f"expected 1 query_processed event, got {len(qp_events)}: {captured_log_events}"
    event = qp_events[0]

    # Cache-hit fields populated.
    assert event["cache_hit"] is True
    assert event["button_type"] == "summary"

    # The B1+B2 fix: cached grounding triplet must be on the event verbatim.
    assert event["grounding_status"] == "grounded"
    assert event["retrieval_count"] == 4
    assert event["retrieval_sources"] == ["bill", "bill-webflow"]

    # Citations were also reconstructed (response replay carries them).
    assert event["has_citations"] is True
    assert event["citations_count"] == 1


@pytest.mark.asyncio
async def test_streaming_cache_hit_legacy_v1_marks_unknown(
    agent_with_buttons_enabled,
    captured_log_events,
    monkeypatch,
):
    """v1 (legacy) entries lack the grounding triplet — the agent must tag
    the event with ``grounding_status="legacy_unknown"`` so the eval script
    excludes them from grounding/citation rate denominators.
    """
    rs = MagicMock()
    rs._client = FakeRedisClient()
    cache = ButtonCache(rs)

    # Seed a legacy v1 entry (no grounding triplet, legacy key prefix).
    legacy_payload = {
        "response": "Old summary text",
        "citations": [],
        "confidence": 0.9,
        "cached_at": "2026-04-25T00:00:00+00:00",
        "button_type": "summary",
    }
    rs._client.store["votebot:button:hr-9999-2025:summary"] = json.dumps(legacy_payload)
    monkeypatch.setattr(
        "votebot.services.button_cache.get_button_cache", lambda: cache
    )

    page_context = PageContext(
        type="bill",
        id="HR-9999",
        jurisdiction="US",
        title="Legacy Bill",
        slug="hr-9999-2025",
    )

    chunks = []
    async for chunk in agent_with_buttons_enabled.process_message_stream(
        message="Summarize this bill",
        session_id="sess-legacy",
        page_context=page_context,
        button="summary",
        visitor_id="v_legacy",
    ):
        chunks.append(chunk)

    import asyncio
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    qp_events = [e for e in captured_log_events if e.get("event_type") == "query_processed"]
    assert len(qp_events) == 1
    event = qp_events[0]

    # Legacy hit signal: grounding_status falls through to "legacy_unknown",
    # retrieval_count stays None (NOT 0 — the eval script uses None to mean
    # "unknown, exclude from retrieval-miss denominator" vs 0 which means
    # "deliberately no retrieval ran, count as miss"). PM v5 build review
    # caught a previous None→0 coercion bug; this assertion locks the fix in.
    assert event["cache_hit"] is True
    assert event["grounding_status"] == "legacy_unknown"
    assert event["retrieval_sources"] is None
    assert event["retrieval_count"] is None


@pytest.mark.asyncio
async def test_non_streaming_cache_hit_preserves_grounding_metadata(
    agent_with_buttons_enabled,
    primed_button_cache,
    captured_log_events,
    cached_v2_payload,
):
    """Plan §1.3 — same B1+B2 fix on the process_message (non-streaming) path.

    PM v5 build review flagged that the streaming branch had its own
    integration test but the non-streaming branch didn't. Both branches
    share _maybe_serve_from_button_cache + _log_query, but they're wired
    independently in the agent — a regression in one wouldn't be caught
    by the other's test. Mirror the streaming assertion set here.
    """
    page_context = PageContext(
        type="bill",
        id="HR-1234",
        jurisdiction="US",
        title="Sample Bill Act",
        slug="hr-1234-2025",
    )

    result = await agent_with_buttons_enabled.process_message(
        message="Summarize this bill",
        session_id="sess-test-non-stream",
        page_context=page_context,
        button="summary",
        visitor_id="v_test",
        conversation_id="conv-1",
        session_message_index=1,
        conversation_message_index=1,
    )

    # Result itself reflects the cached response.
    assert result.cached is True
    assert result.response == cached_v2_payload["response"]

    import asyncio
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    qp_events = [e for e in captured_log_events if e.get("event_type") == "query_processed"]
    assert len(qp_events) == 1, f"expected 1 query_processed event, got {len(qp_events)}"
    event = qp_events[0]

    assert event["cache_hit"] is True
    assert event["button_type"] == "summary"
    # B1+B2 verified on the non-streaming path too.
    assert event["grounding_status"] == "grounded"
    assert event["retrieval_count"] == 4
    assert event["retrieval_sources"] == ["bill", "bill-webflow"]
    assert event["has_citations"] is True
    assert event["citations_count"] == 1


@pytest.mark.asyncio
async def test_non_streaming_cache_hit_legacy_v1_marks_unknown(
    agent_with_buttons_enabled,
    captured_log_events,
    monkeypatch,
):
    """Symmetry coverage for the non-streaming legacy v1 path.

    PM v5 build review v2 spec gap #5: streaming branch had explicit legacy
    coverage; the non-streaming branch did not. Mirroring the assertions
    here closes the gap so a future regression in either branch's legacy
    handling fails the suite.
    """
    rs = MagicMock()
    rs._client = FakeRedisClient()
    cache = ButtonCache(rs)

    # Seed under the legacy v1 prefix (no grounding triplet).
    legacy_payload = {
        "response": "Old non-streaming summary",
        "citations": [],
        "confidence": 0.9,
        "cached_at": "2026-04-25T00:00:00+00:00",
        "button_type": "summary",
    }
    rs._client.store["votebot:button:hr-8888-2025:summary"] = json.dumps(legacy_payload)
    monkeypatch.setattr(
        "votebot.services.button_cache.get_button_cache", lambda: cache
    )

    page_context = PageContext(
        type="bill",
        id="HR-8888",
        jurisdiction="US",
        title="Legacy Non-Streaming Bill",
        slug="hr-8888-2025",
    )

    result = await agent_with_buttons_enabled.process_message(
        message="Summarize this bill",
        session_id="sess-legacy-non-stream",
        page_context=page_context,
        button="summary",
        visitor_id="v_legacy",
    )
    assert result.cached is True
    assert result.response == "Old non-streaming summary"

    import asyncio
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    qp_events = [e for e in captured_log_events if e.get("event_type") == "query_processed"]
    assert len(qp_events) == 1
    event = qp_events[0]

    # Same legacy contract as the streaming test: "legacy_unknown" + None
    # so the eval script (Phase 2) excludes from grounding/retrieval-miss
    # denominators.
    assert event["cache_hit"] is True
    assert event["grounding_status"] == "legacy_unknown"
    assert event["retrieval_sources"] is None
    assert event["retrieval_count"] is None
