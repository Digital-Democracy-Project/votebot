"""Tests for the v1 → v2 dual-read fallback in ButtonCache.

Plan §1.2: KEY_PREFIX bumped to ``votebot:button:v2:``. New writes always
go to v2; on read miss in v2, fall back to read-only lookup against the
legacy v1 prefix so existing entries remain serviceable for the duration
of their 7-day TTL (no user-visible cold-start).

The agent's ``_maybe_serve_from_button_cache`` then tags v1 hits with
``grounding_status="legacy_unknown"`` so the eval script can exclude them
from grounding/citation rate denominators (covered separately in the
agent-level tests).
"""

import json
from unittest.mock import MagicMock

import pytest

from votebot.services.button_cache import (
    ButtonCache,
    KEY_PREFIX,
    LEGACY_KEY_PREFIX_V1,
    make_key,
    make_v1_key,
)


class FakeRedisClient:
    """Minimal in-memory async stand-in — same shape as the one in test_button_cache.py."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.set_calls: list[tuple[str, str]] = []

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.store[key] = value
        self.set_calls.append((key, value))
        return True

    async def delete(self, key: str):
        return 1 if self.store.pop(key, None) is not None else 0


@pytest.fixture
def fake_store():
    rs = MagicMock()
    rs._client = FakeRedisClient()
    return rs


@pytest.fixture
def cache(fake_store):
    return ButtonCache(fake_store)


@pytest.mark.asyncio
async def test_v2_hit_returns_full_schema(cache, fake_store):
    """Direct v2 hit returns the full payload including the grounding triplet."""
    payload = {
        "response": "v2 summary",
        "citations": [{"source": "Congress.gov", "document_id": "bill-1"}],
        "confidence": 0.85,
        "grounding_status": "grounded",
        "retrieval_count": 5,
        "retrieval_sources": ["bill", "bill-webflow"],
    }
    await cache.set("hr-1234", "summary", payload)

    got = await cache.get("hr-1234", "summary")
    assert got is not None
    assert got["response"] == "v2 summary"
    assert got["grounding_status"] == "grounded"
    assert got["retrieval_count"] == 5
    assert got["retrieval_sources"] == ["bill", "bill-webflow"]


@pytest.mark.asyncio
async def test_v2_miss_v1_hit_returns_legacy_payload(cache, fake_store):
    """No v2 entry, but a v1 entry exists — fallback returns the v1 payload as-is."""
    legacy = {
        "response": "v1 summary",
        "citations": [{"source": "Congress.gov", "document_id": "bill-1"}],
        "confidence": 0.9,
        # NOTE: no grounding_status, no retrieval_count, no retrieval_sources
    }
    fake_store._client.store[make_v1_key("hr-1234", "summary")] = json.dumps(legacy)

    got = await cache.get("hr-1234", "summary")
    assert got is not None
    assert got["response"] == "v1 summary"
    # The grounding triplet is absent — caller substitutes "legacy_unknown".
    assert "grounding_status" not in got
    assert "retrieval_count" not in got
    assert "retrieval_sources" not in got


@pytest.mark.asyncio
async def test_v2_miss_v1_miss_returns_none(cache, fake_store):
    """Neither prefix has the key — clean miss."""
    got = await cache.get("nonexistent", "summary")
    assert got is None


@pytest.mark.asyncio
async def test_writes_only_go_to_v2_prefix(cache, fake_store):
    """set() must never write under the legacy v1 prefix (read-only fallback only)."""
    await cache.set("hr-1234", "summary", {"response": "x", "citations": [], "confidence": 0.9})

    # Verify exactly one key was written, and it's under the v2 prefix.
    written_keys = [k for k, _ in fake_store._client.set_calls]
    assert len(written_keys) == 1
    assert written_keys[0] == make_key("hr-1234", "summary")
    assert written_keys[0].startswith(KEY_PREFIX)
    # Sanity: the v2 prefix has the full ":v2:" segment.
    assert written_keys[0] == "votebot:button:v2:hr-1234:summary"
    # And the literal v1 key was NOT written.
    assert make_v1_key("hr-1234", "summary") not in fake_store._client.store


@pytest.mark.asyncio
async def test_v2_takes_precedence_when_both_exist(cache, fake_store):
    """During the migration window, both v1 and v2 entries can coexist for a slug.

    v2 must win — otherwise we'd serve stale legacy data even after a fresh write.
    """
    # Pre-seed a legacy v1 entry, then write the new v2 schema for the same slug.
    fake_store._client.store[make_v1_key("hr-1234", "summary")] = json.dumps(
        {"response": "stale v1", "citations": [], "confidence": 0.7}
    )
    await cache.set(
        "hr-1234",
        "summary",
        {
            "response": "fresh v2",
            "citations": [],
            "confidence": 0.9,
            "grounding_status": "grounded",
            "retrieval_count": 4,
            "retrieval_sources": ["bill"],
        },
    )

    got = await cache.get("hr-1234", "summary")
    assert got is not None
    assert got["response"] == "fresh v2"
    assert got["grounding_status"] == "grounded"
