"""Unit tests for ButtonCache (Phase 2b of PLAN-quick-action-buttons)."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from votebot.services.button_cache import (
    CACHEABLE_TYPES,
    KEY_PREFIX,
    SAFETY_TTL,
    ButtonCache,
    make_key,
)


class FakeRedisClient:
    """In-memory async stand-in for redis.asyncio.Redis."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.last_ex: int | None = None

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.store[key] = value
        self.last_ex = ex
        return True

    async def delete(self, key: str):
        return 1 if self.store.pop(key, None) is not None else 0

    async def scan_iter(self, match: str, count: int = 100):
        # Convert glob to a simple prefix match — sufficient for our tests
        prefix = match.rstrip("*")
        for k in list(self.store.keys()):
            if k.startswith(prefix):
                yield k


@pytest.fixture
def fake_store():
    rs = MagicMock()
    rs._client = FakeRedisClient()
    return rs


@pytest.fixture
def cache(fake_store):
    return ButtonCache(fake_store)


@pytest.mark.asyncio
async def test_set_and_get_summary(cache, fake_store):
    payload = {"response": "Bill summary text", "citations": [], "confidence": 0.9}
    await cache.set("hr-1234-2025", "summary", payload)

    got = await cache.get("hr-1234-2025", "summary")
    assert got is not None
    assert got["response"] == "Bill summary text"
    assert got["button_type"] == "summary"
    assert "cached_at" in got


@pytest.mark.asyncio
async def test_status_votes_is_never_cached(cache, fake_store):
    payload = {"response": "Latest status...", "confidence": 0.9}
    await cache.set("hr-1234-2025", "status_votes", payload)
    # Even though we called set, nothing should land in the store
    assert fake_store._client.store == {}

    # And get always returns None for status_votes
    got = await cache.get("hr-1234-2025", "status_votes")
    assert got is None


@pytest.mark.asyncio
async def test_get_returns_none_on_miss(cache):
    assert await cache.get("not-cached-yet", "summary") is None


@pytest.mark.asyncio
async def test_set_uses_safety_ttl(cache, fake_store):
    await cache.set("hr-1234-2025", "summary", {"response": "x"})
    assert fake_store._client.last_ex == SAFETY_TTL


@pytest.mark.asyncio
async def test_invalidate_bill_clears_all_cacheable_types(cache, fake_store):
    await cache.set("hr-1234-2025", "summary", {"response": "summary"})
    await cache.set("hr-1234-2025", "pros_cons", {"response": "pros"})
    assert len(fake_store._client.store) == 2

    deleted = await cache.invalidate_bill("hr-1234-2025")

    assert deleted == 2
    assert await cache.get("hr-1234-2025", "summary") is None
    assert await cache.get("hr-1234-2025", "pros_cons") is None


@pytest.mark.asyncio
async def test_invalidate_bill_is_idempotent(cache, fake_store):
    # No prior keys
    deleted = await cache.invalidate_bill("nonexistent")
    assert deleted == 0


@pytest.mark.asyncio
async def test_redis_unavailable_no_ops_gracefully():
    # _client = None simulates Redis down
    rs = MagicMock()
    rs._client = None
    cache = ButtonCache(rs)

    assert await cache.get("any", "summary") is None
    await cache.set("any", "summary", {"response": "x"})  # should not raise
    assert await cache.invalidate_bill("any") == 0
    assert await cache.list_cached_keys() == []


@pytest.mark.asyncio
async def test_list_cached_keys_uses_scan(cache, fake_store):
    await cache.set("bill-a", "summary", {"r": 1})
    await cache.set("bill-b", "pros_cons", {"r": 2})
    await cache.set("bill-c", "summary", {"r": 3})

    keys = await cache.list_cached_keys()
    assert sorted(keys) == sorted([
        f"{KEY_PREFIX}bill-a:summary",
        f"{KEY_PREFIX}bill-b:pros_cons",
        f"{KEY_PREFIX}bill-c:summary",
    ])


def test_make_key_uses_v2_prefix():
    """v2 prefix bump (plan §1.2) — new writes always include `:v2:`."""
    assert make_key("hr-1234", "summary") == "votebot:button:v2:hr-1234:summary"
    assert make_key("hr-1234", "pros_cons") == "votebot:button:v2:hr-1234:pros_cons"


def test_cacheable_types_excludes_status_votes():
    assert "summary" in CACHEABLE_TYPES
    assert "pros_cons" in CACHEABLE_TYPES
    assert "status_votes" not in CACHEABLE_TYPES


@pytest.mark.asyncio
async def test_get_handles_missing_optional_citation_fields(cache, fake_store):
    """A cached payload from an older version may lack newer Citation fields.

    Defensive: get() should still return the dict, and downstream code (in
    agent._maybe_serve_from_button_cache) uses .get(... , default) so missing
    fields don't crash citation reconstruction. Stored under the legacy v1
    prefix so the dual-read fallback exercises the same code path.
    """
    # Simulate an old cached payload missing url + relevance_score
    minimal_cached = {
        "response": "old summary text",
        "citations": [
            {"source": "Congress.gov", "document_id": "bill-1", "excerpt": "text"}
            # NOTE: no url, no relevance_score
        ],
        "confidence": 0.8,
        "cached_at": "2026-04-01T00:00:00+00:00",
        "button_type": "summary",
    }
    # Legacy v1 prefix (no ":v2:") — exercises the dual-read fallback in get().
    fake_store._client.store["votebot:button:hr-1234:summary"] = json.dumps(minimal_cached)

    got = await cache.get("hr-1234", "summary")
    assert got is not None
    assert got["response"] == "old summary text"
    # Verify the citation entry is preserved as-is — agent's defensive
    # _maybe_serve_from_button_cache will fill defaults when reconstructing.
    cit = got["citations"][0]
    assert cit["source"] == "Congress.gov"
    assert "url" not in cit  # not present in old payload — OK


@pytest.mark.asyncio
async def test_set_persists_grounding_triplet(cache, fake_store):
    """Plan §1.1 — payload carries grounding_status, retrieval_count, retrieval_sources."""
    payload = {
        "response": "Bill summary",
        "citations": [],
        "confidence": 0.9,
        "grounding_status": "grounded",
        "retrieval_count": 3,
        "retrieval_sources": ["bill", "bill-webflow"],
    }
    await cache.set("hr-1234", "summary", payload)

    got = await cache.get("hr-1234", "summary")
    assert got is not None
    assert got["grounding_status"] == "grounded"
    assert got["retrieval_count"] == 3
    assert got["retrieval_sources"] == ["bill", "bill-webflow"]
