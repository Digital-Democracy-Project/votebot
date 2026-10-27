"""VOTEBOT-52: a deployment with no Webflow configuration makes no Webflow request.

The new deployment sets no `WEBFLOW_*` by design (the broker replaced those lookups). Before this, a page context that
still carried a `slug` made a request to Webflow with an empty key and an empty collection id on every message:
a round trip and a new TLS client, only to be refused.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from votebot.api.schemas.chat import PageContext
from votebot.config import Settings
from votebot.core.agent import VoteBotAgent
from votebot.services import webflow_lookup as wl
from votebot.services.webflow_lookup import WebflowLookupService

REAL = httpx.AsyncClient


@pytest.fixture
def requests(monkeypatch):
    """Records every request a Webflow client would make (served by a stub), so none is a real one."""
    seen: list[httpx.Request] = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(404, json={"message": "not found"})

    monkeypatch.setattr(wl.httpx, "AsyncClient", lambda **kw: REAL(transport=httpx.MockTransport(handler), **kw))
    return seen


def _unconfigured() -> WebflowLookupService:
    return WebflowLookupService(Settings(_env_file=None))


def _configured() -> WebflowLookupService:
    return WebflowLookupService(
        Settings(
            webflow_votebot_api_key="wf-key",
            webflow_bills_collection_id="bills-1",
            webflow_organizations_collection_id="orgs-1",
            webflow_legislators_collection_id="legs-1",
            _env_file=None,
        )
    )


class TestNothingIsAskedOfWebflowWhenItIsNotConfigured:
    async def test_every_lookup_answers_not_found_without_a_request(self, requests):
        svc = _unconfigured()
        assert (await svc.get_bill_details(slug="a-bill")).found is False
        assert (await svc.get_bill_details(webflow_id="wf-1")).found is False
        assert (await svc.get_bill_org_positions(slug="a-bill")).found is False
        assert (await svc.get_org_bill_positions(webflow_id="wf-2")).found is False
        assert (await svc.get_org_details(slug="an-org")).found is False
        assert (await svc.get_legislator_details(slug="a-legislator")).found is False
        assert (await svc.get_legislator_details(webflow_id="wf-3")).found is False
        assert requests == []

    @pytest.mark.parametrize(
        "fetch",
        ["_fetch_bill_by_id", "_fetch_bill_by_slug", "_fetch_org_by_id", "_fetch_org_item_by_id", "_fetch_org_item_by_slug",
         "_fetch_bill_info_by_id", "_fetch_legislator_by_id", "_fetch_legislator_by_slug"],
    )
    async def test_each_request_method_returns_nothing_and_asks_nothing(self, requests, fetch):
        assert await getattr(_unconfigured(), fetch)("anything") is None
        assert requests == []

    async def test_a_key_without_a_collection_id_is_not_enough(self, requests):
        svc = WebflowLookupService(Settings(webflow_votebot_api_key="wf-key", _env_file=None))
        assert await svc._fetch_bill_by_slug("a-bill") is None
        assert requests == []


class TestAConfiguredDeploymentIsUnchanged:
    async def test_the_request_is_still_made_with_the_key_and_the_collection(self, requests):
        svc = _configured()
        assert await svc._fetch_bill_by_id("wf-1") is None  # the stub answers 404: not found, as Webflow would
        assert len(requests) == 1
        assert requests[0].url.path == "/v2/collections/bills-1/items/wf-1"
        assert requests[0].headers["authorization"] == "Bearer wf-key"

    async def test_each_collection_uses_its_own_id(self, requests):
        svc = _configured()
        await svc._fetch_org_by_id("o-1")
        await svc._fetch_legislator_by_id("l-1")
        assert [r.url.path for r in requests] == ["/v2/collections/orgs-1/items/o-1", "/v2/collections/legs-1/items/l-1"]


class TestAPageThatCarriesASlugOnTheNewDeployment:
    async def test_the_bill_lookup_makes_no_request_to_webflow(self, requests):
        agent = VoteBotAgent.__new__(VoteBotAgent)
        agent.settings = Settings(pinecone_index_name="ddp-knowledge-base", _env_file=None)
        agent.webflow_lookup = _unconfigured()
        agent.bill_votes = SimpleNamespace(
            get_bill_info=AsyncMock(return_value=None), get_bill_info_by_url=AsyncMock(return_value=None)
        )
        agent.retrieval = SimpleNamespace(
            vector_store=SimpleNamespace(query=AsyncMock(return_value=[])), _legislative_scope=lambda page: {}
        )
        page = PageContext(type="bill", id="HB 1", slug="a-bill", jurisdiction="FL", session="2026")
        await agent._prefetch_bill_info("What is the status of this bill?", page, None)
        assert requests == []  # before: a request to api.webflow.com with an empty key, on every such message
