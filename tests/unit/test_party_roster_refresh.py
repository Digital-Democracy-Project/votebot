"""VOTEBOT-48: a visitor never waits for the party roster download once the process knows the roster.

An expired roster is served at once while one background refresh fetches the new one. A failed or partial refresh
changes nothing. Only a jurisdiction the process has not seen yet is fetched inline (the lifespan warms the federal one).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from votebot.config import Settings
from votebot.services import bill_votes as bv
from votebot.services.bill_votes import PARTY_CACHE_SECONDS, PARTY_RETRY_SECONDS, BillVotesService


class FakeApi:
    """api-v3's /people: `roster` is {"upper": [(name, party)], "lower": [...]}; `status` and `boom` make it fail."""

    def __init__(self, roster, delay=0.0):
        self.roster = roster
        self.delay = delay
        self.status = 200
        self.boom = False
        self.requests = 0

    def client(self, **kwargs):
        api = self

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None, params=None):
                api.requests += 1
                await asyncio.sleep(api.delay)
                if api.boom:
                    raise RuntimeError("api-v3 down")
                people = api.roster[params["org_classification"]]
                body = {"results": [{"name": n, "party": p} for n, p in people], "pagination": {"total_items": len(people)}}
                return type("R", (), {"status_code": api.status, "json": lambda self: body})()

        return Client()


def _service(api: FakeApi, monkeypatch) -> BillVotesService:
    monkeypatch.setattr(bv.httpx, "AsyncClient", api.client)
    settings = Settings(ddp_openstates_api_root="http://api-v3.test", ddp_openstates_bearer_token="k", _env_file=None)
    return BillVotesService(settings)


def _age(service: BillVotesService, seconds: float, jurisdiction: str = "us") -> None:
    key = f"legislators-{jurisdiction}"
    stored, roster = service._legislator_cache[key]
    service._legislator_cache[key] = (stored - seconds, roster)


async def _lookup(service, jurisdiction="us"):
    return await service._get_legislator_parties(jurisdiction, FakeApi({}).client(), {})


ROSTER = {"upper": [("Ann Upper", "Democratic")], "lower": [("Bob Lower", "Republican")]}


class TestTheFirstLookupIsStillInline:
    async def test_an_unknown_jurisdiction_is_fetched_and_remembered(self, monkeypatch):
        api = FakeApi(ROSTER)
        service = _service(api, monkeypatch)
        assert await service._get_legislator_parties("fl", api.client(), {}) == {"ann upper": "Democratic", "bob lower": "Republican"}
        first = api.requests
        await service._get_legislator_parties("fl", api.client(), {})
        assert api.requests == first  # remembered

    async def test_a_partial_first_fetch_answers_once_and_is_not_remembered(self, monkeypatch):
        api = FakeApi(ROSTER)
        service = _service(api, monkeypatch)
        api.status = 500
        assert await service._get_legislator_parties("fl", api.client(), {}) == {}
        assert "legislators-fl" not in service._legislator_cache  # asked again next time

    async def test_a_jurisdiction_with_no_such_chamber_is_an_empty_roster_that_is_remembered(self, monkeypatch):
        api = FakeApi({"upper": [], "lower": []})  # api-v3 answers 200 and an empty list
        service = _service(api, monkeypatch)
        assert await service._get_legislator_parties("ne", api.client(), {}) == {}
        assert "legislators-ne" in service._legislator_cache


class TestAnExpiredRosterIsServedAtOnce:
    async def test_it_returns_the_old_roster_without_waiting_for_the_download(self, monkeypatch):
        api = FakeApi(ROSTER, delay=0.2)
        service = _service(api, monkeypatch)
        await service._refresh_parties("us")
        _age(service, PARTY_CACHE_SECONDS + 1)
        began = time.perf_counter()
        got = await _lookup(service)
        assert time.perf_counter() - began < 0.05  # the refresh takes 0.4 s; the visitor does not wait for it
        assert got == {"ann upper": "Democratic", "bob lower": "Republican"}
        await asyncio.gather(*service._refreshing.values())

    async def test_many_lookups_start_exactly_one_refresh(self, monkeypatch):
        api = FakeApi(ROSTER, delay=0.05)
        service = _service(api, monkeypatch)
        await service._refresh_parties("us")
        before = api.requests
        _age(service, PARTY_CACHE_SECONDS + 1)
        await asyncio.gather(*[_lookup(service) for _ in range(5)])
        assert len(service._refreshing) == 1
        await asyncio.gather(*service._refreshing.values())
        assert api.requests - before == 2  # one refresh: an upper and a lower page, not five of them

    async def test_a_party_change_is_picked_up_once_the_refresh_has_run(self, monkeypatch):
        api = FakeApi(ROSTER)
        service = _service(api, monkeypatch)
        await service._refresh_parties("us")
        api.roster = {"upper": [("Ann Upper", "Republican")], "lower": [("Bob Lower", "Republican")]}
        _age(service, PARTY_CACHE_SECONDS + 1)
        assert (await _lookup(service))["ann upper"] == "Democratic"  # still the old one while it refreshes
        await asyncio.gather(*service._refreshing.values())
        assert (await _lookup(service))["ann upper"] == "Republican"  # the next lookup has the new party
        assert not service._refreshing

    async def test_a_fresh_roster_starts_no_refresh(self, monkeypatch):
        api = FakeApi(ROSTER)
        service = _service(api, monkeypatch)
        await service._refresh_parties("us")
        before = api.requests
        await _lookup(service)
        assert not service._refreshing and api.requests == before


class TestARefreshThatGoesWrongChangesNothing:
    @pytest.mark.parametrize("failure", ["boom", "status", "empty"])
    async def test_the_old_roster_keeps_serving_and_the_visitor_sees_no_error(self, monkeypatch, failure):
        api = FakeApi(ROSTER)
        service = _service(api, monkeypatch)
        await service._refresh_parties("us")
        old = dict(service._legislator_cache["legislators-us"][1])
        _age(service, PARTY_CACHE_SECONDS + 1)
        if failure == "boom":
            api.boom = True
        elif failure == "status":
            api.status = 503
        else:
            api.roster = {"upper": [], "lower": []}  # a "complete" but empty answer is not a roster
        assert await _lookup(service) == old
        await asyncio.gather(*service._refreshing.values())
        assert await _lookup(service) == old  # still the old roster, never an empty one

    async def test_it_is_tried_again_after_the_retry_delay_not_on_every_message(self, monkeypatch):
        api = FakeApi(ROSTER)
        service = _service(api, monkeypatch)
        await service._refresh_parties("us")
        _age(service, PARTY_CACHE_SECONDS + 1)
        api.boom = True
        await _lookup(service)
        await asyncio.gather(*service._refreshing.values())
        attempts = api.requests
        await _lookup(service)  # right after the failure: no new attempt
        assert not service._refreshing and api.requests == attempts
        _age(service, PARTY_RETRY_SECONDS + 1)
        api.boom = False
        await _lookup(service)  # after the delay: one more
        await asyncio.gather(*service._refreshing.values())
        assert api.requests > attempts


class TestWarmingTheFederalRoster:
    async def test_warming_fetches_it_in_the_background_and_a_chat_then_finds_it_ready(self, monkeypatch):
        api = FakeApi(ROSTER, delay=0.05)
        service = _service(api, monkeypatch)
        task = service.warm_parties("us")
        assert not task.done()  # the caller is not kept waiting
        assert service.warm_parties("us") is task  # asking again joins the running one
        await task
        before = api.requests
        assert (await _lookup(service))["bob lower"] == "Republican"
        assert api.requests == before  # no download on the chat's path

    async def test_a_warm_up_that_fails_is_silent(self, monkeypatch):
        api = FakeApi(ROSTER)
        api.boom = True
        service = _service(api, monkeypatch)
        await service.warm_parties("us")
        assert "legislators-us" not in service._legislator_cache
