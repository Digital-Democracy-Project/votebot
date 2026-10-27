"""VOTEBOT-50: OpenStates calls share one long-lived HTTP client, and older sessions are asked together.

A counting MockTransport stands in for api-v3. The client's construction is what is counted: a client that is made once
is one connection pool, so repeated lookups reuse open connections instead of starting a TCP and TLS handshake each time.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from votebot.config import Settings
from votebot.services import bill_votes as bv
from votebot.services.bill_votes import BillVotesService

REAL = httpx.AsyncClient


class Api:
    """api-v3's /bills/{jurisdiction}/{session}/{id} and /people. `has` is the sessions that have the bill."""

    def __init__(self, has=("2026",), delay=None, fail=()):
        self.has = set(has)
        self.delay = delay or {}
        self.fail = set(fail)
        self.requests: list[httpx.Request] = []
        self.clients: list[dict] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.cancelled: list[str] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        parts = request.url.path.strip("/").split("/")
        if parts[-1] == "people":
            return httpx.Response(200, json={"results": [{"name": "Ann", "party": "Democratic"}], "pagination": {"total_items": 1}})
        session = parts[-2]
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay.get(session, 0))
        except asyncio.CancelledError:
            self.cancelled.append(session)
            raise
        finally:
            self.in_flight -= 1
        if session in self.fail:
            return httpx.Response(500, json={"detail": "down"})
        if session not in self.has:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json={"identifier": "HB 1234", "title": f"Bill {session}", "votes": [], "sponsorships": [], "actions": []})

    def factory(self, **kwargs):
        self.clients.append(kwargs)
        return REAL(transport=httpx.MockTransport(self.handler), **kwargs)


def _service(api: Api, monkeypatch, **settings) -> BillVotesService:
    monkeypatch.setattr(bv.httpx, "AsyncClient", api.factory)
    cfg = {"ddp_openstates_api_root": "http://api-v3.test", "ddp_openstates_bearer_token": "tok", "_env_file": None, **settings}
    return BillVotesService(Settings(**cfg))


class TestOneLongLivedClient:
    async def test_repeated_lookups_make_one_client(self, monkeypatch):
        api = Api()
        service = _service(api, monkeypatch)
        for _ in range(3):
            await service.get_bill_votes("fl", "2026", "HB 1234")
        await service.get_bill_info("fl", "2026", "HB 1234")
        await service.get_bill_info("fl", "2026", "HB 1234")
        assert len(api.clients) == 1  # one connection pool for all five lookups (and their roster requests)
        assert len(api.requests) > 5

    async def test_the_timeout_is_kept_and_the_pool_is_sized_for_a_hundred_chats(self, monkeypatch):
        api = Api()
        service = _service(api, monkeypatch)
        await service.get_bill_votes("fl", "2026", "HB 1234")
        kwargs = api.clients[0]
        assert kwargs["timeout"] == 30.0
        assert kwargs["limits"].max_connections >= 100  # a hundred simultaneous chats do not queue for a connection
        assert kwargs["limits"].max_keepalive_connections >= 20

    @pytest.mark.parametrize(
        ("auth_header", "expected"),
        [("bearer", {"authorization": "Bearer tok"}), ("x-api-key", {"x-api-key": "tok"})],
    )
    async def test_the_credential_is_chosen_per_request_and_exactly_one_shape_is_sent(self, monkeypatch, auth_header, expected):
        api = Api()
        service = _service(api, monkeypatch, ddp_openstates_auth_header=auth_header)
        await service.get_bill_votes("fl", "2026", "HB 1234")
        await service.get_bill_votes("fl", "2026", "HB 1234")
        assert "headers" not in api.clients[0]  # nothing is baked into the client
        for request in api.requests:
            shapes = {h for h in ("authorization", "x-api-key") if h in request.headers}
            assert shapes == set(expected)
            assert all(request.headers[k] == v for k, v in expected.items())

    async def test_closing_closes_the_client_and_a_later_lookup_makes_a_new_one(self, monkeypatch):
        api = Api()
        service = _service(api, monkeypatch)
        await service.get_bill_votes("fl", "2026", "HB 1234")
        client = service._http
        await service.close()
        assert client.is_closed and service._http is None
        await service.close()  # closing twice is harmless
        await service.get_bill_votes("fl", "2026", "HB 1234")
        assert len(api.clients) == 2

    async def test_a_client_of_a_loop_that_has_ended_is_replaced(self, monkeypatch):
        api = Api()
        service = _service(api, monkeypatch)
        await service.get_bill_votes("fl", "2026", "HB 1234")
        ended = asyncio.new_event_loop()
        ended.close()
        service._http_loop = ended  # as if the service had been used on a loop that is gone
        await service.get_bill_votes("fl", "2026", "HB 1234")
        assert len(api.clients) == 2
        assert service._http_loop is asyncio.get_running_loop()

    async def test_a_client_that_belongs_to_another_running_loop_is_refused_not_orphaned(self, monkeypatch):
        api = Api()
        service = _service(api, monkeypatch)
        await service.get_bill_votes("fl", "2026", "HB 1234")
        other = asyncio.new_event_loop()  # still open
        try:
            service._http_loop = other
            with pytest.raises(RuntimeError, match="another running event loop"):
                async with service._client():
                    pass
            assert len(api.clients) == 1  # nothing was built and nothing was dropped
        finally:
            other.close()

    async def test_lookups_at_the_same_time_share_the_client(self, monkeypatch):
        api = Api(delay={"2026": 0.02})
        service = _service(api, monkeypatch)
        await asyncio.gather(*[service.get_bill_votes("fl", "2026", "HB 1234") for _ in range(20)])
        assert len(api.clients) == 1


class TestOlderSessionsAreAskedTogether:
    async def test_the_specified_session_alone_when_it_has_the_bill(self, monkeypatch):
        api = Api(has=("2026",))
        result = await _service(api, monkeypatch).get_bill_info("fl", "2026", "HB 1234")
        assert result.found and result.session == "2026"
        bill_requests = [r.url.path for r in api.requests if "/people" not in r.url.path]
        assert bill_requests == ["/bills/fl/2026/HB1234"]  # no older session was asked

    async def test_the_newest_older_session_wins_when_both_have_it(self, monkeypatch):
        api = Api(has=("2025", "2024"))
        result = await _service(api, monkeypatch).get_bill_info("fl", "2026", "HB 1234")
        assert result.found and result.session == "2025"

    async def test_an_older_session_that_only_the_oldest_has(self, monkeypatch):
        api = Api(has=("2024",))
        result = await _service(api, monkeypatch).get_bill_info("fl", "2026", "HB 1234")
        assert result.found and result.session == "2024"

    async def test_not_found_anywhere_is_the_same_not_found_result(self, monkeypatch):
        api = Api(has=())
        result = await _service(api, monkeypatch).get_bill_info("fl", "2026", "HB 1234")
        assert result.found is False and result.session == "2026" and result.bill_identifier == "HB 1234"

    async def test_a_failing_specified_session_falls_back_to_the_older_ones_as_before(self, monkeypatch):
        api = Api(has=("2025",), fail=("2026",))
        result = await _service(api, monkeypatch).get_bill_info("fl", "2026", "HB 1234")
        assert result.found and result.session == "2025"

    async def test_the_two_older_sessions_are_asked_at_the_same_time(self, monkeypatch):
        api = Api(has=("2024",), delay={"2025": 0.1, "2024": 0.1})
        began = time.perf_counter()
        result = await _service(api, monkeypatch).get_bill_info("fl", "2026", "HB 1234")
        assert result.session == "2024"
        assert api.max_in_flight == 2
        assert time.perf_counter() - began < 0.18  # 0.2 s one after another

    async def test_an_older_session_still_running_is_cancelled_once_a_newer_one_has_the_bill(self, monkeypatch):
        api = Api(has=("2025", "2024"), delay={"2025": 0.01, "2024": 5})
        began = time.perf_counter()
        result = await _service(api, monkeypatch).get_bill_info("fl", "2026", "HB 1234")
        assert result.session == "2025"
        assert time.perf_counter() - began < 1  # did not wait for the 5 s request
        assert api.cancelled == ["2024"] and api.in_flight == 0

    async def test_the_shared_client_still_works_after_an_older_session_request_was_cancelled(self, monkeypatch):
        api = Api(has=("2025", "2024"), delay={"2025": 0.01, "2024": 5})
        service = _service(api, monkeypatch)
        assert (await service.get_bill_info("fl", "2026", "HB 1234")).session == "2025"  # 2024's request is cancelled
        again = await service.get_bill_votes("fl", "2025", "HB 1234")
        assert again is not None and again.title == "Bill 2025"
        assert len(api.clients) == 1  # the same client, still usable

    async def test_the_federal_jurisdiction_has_no_older_sessions(self, monkeypatch):
        api = Api(has=())
        result = await _service(api, monkeypatch).get_bill_info("us", "119", "HR 1")
        assert result.found is False
        assert [r.url.path for r in api.requests] == ["/bills/us/119/HR1"]

    async def test_a_session_that_is_not_a_year_has_no_older_ones_either(self, monkeypatch):
        api = Api(has=())
        await _service(api, monkeypatch).get_bill_info("fl", "2026D", "HB 1")
        assert [r.url.path for r in api.requests] == ["/bills/fl/2026D/HB1"]


class TestClosingAtShutdown:
    async def test_the_shared_services_client_is_closed_when_the_app_stops(self, monkeypatch):
        from types import SimpleNamespace

        from votebot.core import agent as agent_module

        api = Api()
        service = _service(api, monkeypatch)
        await service.get_bill_votes("fl", "2026", "HB 1234")
        client = service._http
        settings = Settings(_env_file=None)
        monkeypatch.setattr(agent_module, "_shared_services", (settings, SimpleNamespace(bill_votes=service)))
        await agent_module.close_services(settings)
        assert client.is_closed

    async def test_nothing_is_built_just_to_be_closed(self, monkeypatch):
        from votebot.core import agent as agent_module

        monkeypatch.setattr(agent_module, "_shared_services", None)
        monkeypatch.setattr(agent_module, "_services_for", lambda s: (_ for _ in ()).throw(AssertionError("built")))
        await agent_module.close_services(Settings(_env_file=None))  # no message was ever served: nothing to close
