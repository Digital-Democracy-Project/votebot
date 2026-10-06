"""VOTEBOT-15: canonical-index parity for organizations, title lookup and the button cache."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.unit.test_ocd_bill_id_retrieval import BILL, NEW_INDEX, _hit, _matches, _service
from votebot.api.schemas.chat import PageContext
from votebot.config import LEGACY_PINECONE_INDEX_NAME
from votebot.core.agent import VoteBotAgent
from votebot.services import button_cache
from votebot.services.bill_versions import BillVersion
from votebot.services.button_cache import ButtonCache, make_key


class TestOrganizationRetrieval:
    def test_canonical_org_page_filters_by_the_broker_org_id(self):
        ctx = PageContext(type="organization", id="42", slug="some-org", webflow_id="wf1")
        assert _service()._build_filters(ctx) == {"broker_org_id": "42"}

    def test_canonical_org_page_with_a_webflow_style_id_falls_back_to_the_slug(self):
        # /content/resolve's Webflow path returns id=slug, which is not a broker id
        ctx = PageContext(type="organization", id="some-org", slug="some-org")
        assert _service()._build_filters(ctx) == {"slug": "some-org"}

    def test_canonical_org_page_with_nothing_to_pin_has_no_filter(self):
        assert _service()._build_filters(PageContext(type="organization", webflow_id="wf1")) == {}

    def test_legacy_org_filters_are_unchanged(self):
        svc = _service(LEGACY_PINECONE_INDEX_NAME)
        assert svc._build_filters(PageContext(type="organization", id="42", webflow_id="wf1")) == {
            "webflow_id": "wf1"
        }
        assert svc._build_filters(PageContext(type="organization", id="42", slug="s")) == {"slug": "s"}

    async def test_org_retrieval_on_the_canonical_index_returns_only_that_org(self):
        org_chunks = {
            "42": _hit("organization", broker_org_id="42", document_id="organization:42"),
            "7": _hit("organization", broker_org_id="7", document_id="organization:7"),
        }
        calls: list = []

        def respond(f):
            return [h for h in org_chunks.values() if _matches(h.metadata, f)]

        svc = _service(queries=calls, respond=respond)
        result = await svc.retrieve("what does this org do", PageContext(type="organization", id="42"))

        assert result.chunks and {c.metadata["broker_org_id"] for c in result.chunks} == {"42"}
        assert result.filters_applied == {"broker_org_id": "42"}
        assert calls[0] == {"document_type": "organization", "broker_org_id": "42"}

    async def test_legacy_org_retrieval_still_filters_by_webflow_id(self):
        calls: list = []
        svc = _service(LEGACY_PINECONE_INDEX_NAME, queries=calls)
        await svc.retrieve("what does this org do", PageContext(type="organization", webflow_id="wf1"))
        assert calls[0] == {"document_type": "organization", "webflow_id": "wf1"}


def _agent(index: str = NEW_INDEX, results=None, scope=None) -> VoteBotAgent:
    a = VoteBotAgent.__new__(VoteBotAgent)
    a.settings = SimpleNamespace(bill_filter_key="ocd_bill_id" if index == NEW_INDEX else "webflow_id")
    a.retrieval = SimpleNamespace(_legislative_scope=lambda ctx: scope or {})
    queries: list = []

    async def query(query, top_k=10, filter=None, include_metadata=True):
        queries.append(filter)
        return results or []

    a.bill_votes = SimpleNamespace(vector_store=SimpleNamespace(query=query))
    a.queries = queries
    return a


class TestResolveBillFromTitle:
    async def test_canonical_index_searches_bill_text_and_reads_the_gov_id(self):
        hit = _hit("bill-text", ocd_bill_id=BILL, gov_id="HB 363", jurisdiction="FL")
        hit.score = 0.9
        agent = _agent(results=[hit])
        assert await agent._resolve_bill_from_title("what does the education funding act do") == ("HB363", "FL")
        assert agent.queries == [{"document_type": "bill-text"}]

    async def test_canonical_index_limits_the_search_to_a_general_pages_jurisdiction(self):
        agent = _agent(scope={"jurisdiction": "FL", "session_code": "2026"})
        await agent._resolve_bill_from_title("what does the education funding act do", PageContext(type="general"))
        assert agent.queries == [{"document_type": "bill-text", "jurisdiction": "FL", "session_code": "2026"}]

    async def test_a_weak_match_resolves_nothing(self):
        hit = _hit("bill-text", ocd_bill_id=BILL, gov_id="HB 363", jurisdiction="FL")
        hit.score = 0.5
        assert await _agent(results=[hit])._resolve_bill_from_title("the education funding act") == (None, None)

    async def test_legacy_index_still_searches_bill_summaries(self):
        hit = _hit("bill", bill_prefix="HR", bill_number="1", jurisdiction="us")
        hit.score = 0.9
        agent = _agent(LEGACY_PINECONE_INDEX_NAME, results=[hit])
        assert await agent._resolve_bill_from_title("the one big beautiful bill act") == ("HR1", "us")
        assert agent.queries == [{"document_type": "bill"}]

    async def test_a_message_without_a_bill_term_searches_nothing(self):
        agent = _agent()
        assert await agent._resolve_bill_from_title("how is the weather") == (None, None)
        assert agent.queries == []


class FakeRedisClient:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        return True

    async def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0


class _Versions:
    def __init__(self, versions):
        self.versions = versions

    async def get_versions(self, ocd_bill_id):
        return self.versions


def _version(document_id: str | None, stage: str = "introduced") -> BillVersion:
    return BillVersion(document_id=document_id, note="Introduced", date="2026-01-05", stage=stage, ordinal=1)


@pytest.fixture
def redis():
    client = FakeRedisClient()
    store = MagicMock()
    store._client = client
    return SimpleNamespace(client=client, cache=ButtonCache(store))


@pytest.fixture
def use_cache(monkeypatch, redis):
    monkeypatch.setattr(button_cache, "get_button_cache", lambda: redis.cache)
    return redis


def _button_agent(index: str = NEW_INDEX, versions=None) -> VoteBotAgent:
    a = _agent(index)
    a.retrieval = SimpleNamespace(bill_versions=_Versions(versions))
    return a


async def _populate(agent, ctx, text="summary text"):
    await agent._populate_button_cache(
        page_context=ctx, button="summary", response_text=text, citations=[], confidence=0.9,
        grounding_status="grounded", retrieval_count=3, retrieval_sources=["bill-text"],
    )


class TestButtonCacheOnTheCanonicalIndex:
    async def test_a_slugless_bill_page_fills_and_hits_the_cache_keyed_by_ocd_bill_id(self, use_cache):
        agent = _button_agent(versions=[_version("11")])
        ctx = PageContext(type="bill", ocd_bill_id=BILL)
        await _populate(agent, ctx)

        assert list(use_cache.client.store) == [make_key(BILL, "summary")]
        stored = json.loads(use_cache.client.store[make_key(BILL, "summary")])
        assert stored["document_id"] == "11"
        hit = await agent._maybe_serve_from_button_cache(page_context=ctx, button="summary")
        assert hit and hit["response"] == "summary text"

    async def test_a_new_current_version_makes_the_entry_stale(self, use_cache):
        ctx = PageContext(type="bill", ocd_bill_id=BILL)
        await _populate(_button_agent(versions=[_version("11")]), ctx)
        # api-v3 now reports a newer version as current
        newer = _button_agent(versions=[_version("11"), _version("12", "amendment")])
        assert await newer._maybe_serve_from_button_cache(page_context=ctx, button="summary") is None
        # ...and the regenerated answer replaces it
        await _populate(newer, ctx, text="newer summary")
        hit = await newer._maybe_serve_from_button_cache(page_context=ctx, button="summary")
        assert hit["response"] == "newer summary"

    @pytest.mark.parametrize("versions", [None, [], [_version(None)], [_version("5", "unknown")]])
    async def test_no_known_current_version_means_no_caching(self, use_cache, versions):
        agent = _button_agent(versions=versions)
        ctx = PageContext(type="bill", ocd_bill_id=BILL)
        await _populate(agent, ctx)
        assert use_cache.client.store == {}
        assert await agent._maybe_serve_from_button_cache(page_context=ctx, button="summary") is None

    @pytest.mark.parametrize("failure", [RuntimeError("api-v3 exploded"), TimeoutError(), AttributeError("bad body")])
    async def test_a_version_lookup_that_raises_bypasses_the_cache_instead_of_failing(self, use_cache, failure):
        agent = _button_agent()
        agent.retrieval = SimpleNamespace(bill_versions=SimpleNamespace(get_versions=AsyncMock(side_effect=failure)))
        ctx = PageContext(type="bill", ocd_bill_id=BILL)
        await _populate(agent, ctx)
        assert use_cache.client.store == {}
        assert await agent._maybe_serve_from_button_cache(page_context=ctx, button="summary") is None

    async def test_a_page_without_an_ocd_bill_id_is_not_cached(self, use_cache):
        agent = _button_agent(versions=[_version("11")])
        await _populate(agent, PageContext(type="bill", slug="a-slug"))
        assert use_cache.client.store == {}

    async def test_status_votes_is_never_cached(self, use_cache):
        agent = _button_agent(versions=[_version("11")])
        ctx = PageContext(type="bill", ocd_bill_id=BILL)
        await agent._populate_button_cache(
            page_context=ctx, button="status_votes", response_text="x", citations=[], confidence=0.9,
            grounding_status="grounded", retrieval_count=1, retrieval_sources=None,
        )
        assert use_cache.client.store == {}

    async def test_the_admin_invalidation_works_with_the_new_key(self, use_cache):
        agent = _button_agent(versions=[_version("11")])
        await _populate(agent, PageContext(type="bill", ocd_bill_id=BILL))
        assert await use_cache.cache.invalidate_bill(BILL) == 1
        assert use_cache.client.store == {}


class TestButtonCacheOnTheLegacyIndex:
    async def test_still_keyed_by_slug_with_no_version_check(self, use_cache):
        agent = _button_agent(LEGACY_PINECONE_INDEX_NAME, versions=None)
        ctx = PageContext(type="bill", slug="hr-1-2025", webflow_id="wf1")
        await _populate(agent, ctx)

        assert list(use_cache.client.store) == [make_key("hr-1-2025", "summary")]
        assert "document_id" not in json.loads(use_cache.client.store[make_key("hr-1-2025", "summary")])
        hit = await agent._maybe_serve_from_button_cache(page_context=ctx, button="summary")
        assert hit and hit["response"] == "summary text"

    async def test_a_page_without_a_slug_is_still_not_cached(self, use_cache):
        agent = _button_agent(LEGACY_PINECONE_INDEX_NAME)
        await _populate(agent, PageContext(type="bill", ocd_bill_id=BILL))
        assert use_cache.client.store == {}
