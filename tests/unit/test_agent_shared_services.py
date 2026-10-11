"""VOTEBOT-37: the routes build a VoteBotAgent per message, so its services must not be rebuilt (and their caches lost) with it."""

import time
from unittest.mock import AsyncMock, patch

from votebot.config import Settings
from votebot.core import agent as agent_module
from votebot.core.agent import VoteBotAgent
from votebot.services import bill_votes as bill_votes_module
from votebot.services.bill_votes import BillVotesService


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, openai_api_key="k", pinecone_api_key="k", api_key="k", **overrides)


def test_two_agents_on_the_same_settings_share_their_services_but_not_the_webflow_lookup():
    s = settings()
    a, b = VoteBotAgent(s), VoteBotAgent(s)
    for name in ("llm", "retrieval", "web_search", "bill_votes", "broker_lookup", "legislators"):
        assert getattr(a, name) is getattr(b, name), name
    assert a.retrieval.vector_store is b.retrieval.vector_store
    assert a.webflow_lookup is not b.webflow_lookup  # its caches never expire: not shared


def test_other_settings_get_services_of_their_own():
    first, second = VoteBotAgent(settings()), VoteBotAgent(settings())
    assert first.llm is not second.llm and first.retrieval is not second.retrieval


async def test_the_bill_version_lookup_is_remembered_from_one_message_to_the_next():
    s = settings(use_ddp_openstates_replica=True, ddp_openstates_api_root="http://api-v3.test")
    fetch = AsyncMock(return_value={"versions": []})
    with patch("votebot.services.bill_versions.BillVersionService._fetch", fetch):
        await VoteBotAgent(s).retrieval.bill_versions.get_versions("bill-1")  # message 1
        await VoteBotAgent(s).retrieval.bill_versions.get_versions("bill-1")  # message 2: a new agent
    assert fetch.await_count == 1


def test_the_shared_set_is_per_settings_object_not_per_process_forever():
    s = settings()
    VoteBotAgent(s)
    assert agent_module._shared_services[0] is s
    VoteBotAgent(settings())
    assert agent_module._shared_services[0] is not s


async def test_party_lookups_are_remembered_for_an_hour_and_then_asked_again():
    service = BillVotesService(settings())
    calls = []

    class Client:
        async def get(self, url, headers=None, params=None):
            calls.append(params["org_classification"])
            return type("R", (), {"status_code": 200, "json": lambda self: {"results": [{"name": "Ann", "party": "Democratic"}], "pagination": {"total_items": 1}}})()

    assert (await service._get_legislator_parties("fl", Client(), {}))["ann"] == "Democratic"
    first_round = len(calls)
    await service._get_legislator_parties("fl", Client(), {})
    assert len(calls) == first_round  # remembered

    later = time.monotonic() + bill_votes_module.PARTY_CACHE_SECONDS + 1
    with patch("votebot.services.bill_votes.time.monotonic", return_value=later):
        await service._get_legislator_parties("fl", Client(), {})
    assert len(calls) == 2 * first_round  # expired: asked again
