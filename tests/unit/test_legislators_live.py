"""VOTEBOT-15: legislator questions are answered live from api-v3's /people.

Legislators are not embedded in the canonical-id index (SYNC-94). api-v3's /people takes `name`,
`id`, `jurisdiction` and `include`; these tests use its response shape (read from ddp-open-states'
api-v3 `people.py` and `schemas.py`).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from tests.unit.test_ocd_bill_id_retrieval import NEW_INDEX
from votebot.api.schemas.chat import PageContext
from votebot.config import Settings
from votebot.core.agent import LEGISLATOR_CUES, VoteBotAgent
from votebot.services import legislators as legislators_module
from votebot.services.legislators import Legislator, LegislatorLookupService, format_legislators

PERSON_ID = "ocd-person/11111111-2222-3333-4444-555555555555"
REAL_CLIENT = httpx.AsyncClient


def _raw(name="Ashley Moody", party="Republican", **extra):
    return {
        "id": PERSON_ID, "name": name, "party": party,
        "current_role": {"title": "Senator", "org_classification": "upper", "district": "FL", "division_id": "x"},
        "jurisdiction": {"id": "ocd-jurisdiction/country:us/government", "name": "United States", "classification": "country"},
        "email": "sen@moody.test", "openstates_url": "https://openstates.org/person/moody/",
        "offices": [{"name": "Capitol Office", "address": "1 Main St; Washington DC", "voice": "202-555-0100", "classification": "capitol"}],
        "links": [{"url": "https://moody.senate.gov", "note": "homepage"}],
        **extra,
    }


def _service(monkeypatch, handler) -> LegislatorLookupService:
    monkeypatch.setattr(
        legislators_module.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)
    )
    settings = Settings(use_ddp_openstates_replica=True, ddp_openstates_api_root="https://api.test", _env_file=None)
    return LegislatorLookupService(settings)


class TestLookupService:
    async def test_by_name_sends_name_includes_and_the_bearer_token(self, monkeypatch):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"results": [_raw()]})

        (person,) = await _service(monkeypatch, handler).find_by_name("Ashley Moody", "us")
        request = seen[0]
        assert request.url.path == "/people"
        params = request.url.params
        assert params["name"] == "Ashley Moody" and params["jurisdiction"] == "us"
        assert sorted(params.get_list("include")) == ["links", "offices"]
        assert request.headers["authorization"].startswith("Bearer ")
        assert (person.name, person.party, person.title, person.chamber, person.district, person.jurisdiction) == (
            "Ashley Moody", "Republican", "Senator", "Senate", "FL", "United States")
        assert person.offices == ["Capitol Office: 1 Main St; Washington DC; 202-555-0100"]
        assert person.links == [("homepage", "https://moody.senate.gov")] and person.current

    async def test_by_id(self, monkeypatch):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"results": [_raw()]})

        person = await _service(monkeypatch, handler).find_by_id(PERSON_ID)
        assert person.person_id == PERSON_ID and seen[0].url.params["id"] == PERSON_ID

    async def test_a_former_legislator_has_no_current_role(self, monkeypatch):
        raw = _raw()
        raw["current_role"] = None
        (person,) = await _service(monkeypatch, lambda r: httpx.Response(200, json={"results": [raw]})).find_by_name("X")
        assert person.current is False and person.title == ""

    @pytest.mark.parametrize("answer", [httpx.Response(500), httpx.Response(200, content=b"<html>"),
                                        httpx.Response(200, json=[1]), httpx.Response(200, json={"results": "x"})])
    async def test_anything_unusable_is_none(self, monkeypatch, answer):
        service = _service(monkeypatch, lambda r: answer)
        assert await service.find_by_name("Ashley Moody") is None
        assert await service.find_by_id(PERSON_ID) is None

    async def test_unusable_rows_are_skipped_and_an_unreachable_service_is_none(self, monkeypatch):
        rows = [_raw(), {"id": "x"}, "junk", {"name": "No id"}]
        people = await _service(monkeypatch, lambda r: httpx.Response(200, json={"results": rows})).find_by_name("X")
        assert [p.name for p in people] == ["Ashley Moody"]

        def down(request):
            raise httpx.ConnectError("down")

        assert await _service(monkeypatch, down).find_by_name("X") is None


class TestFormatting:
    def _p(self, **kw):
        base = {"person_id": PERSON_ID, "name": "Ashley Moody", "party": "Republican", "title": "Senator", "chamber": "Senate",
                "district": "FL", "jurisdiction": "United States", "current": True}
        return Legislator(**{**base, **kw})

    def test_one_match_is_a_full_profile(self):
        text = format_legislators([self._p(email="e@x.test", offices=["Capitol Office: 1 Main St"],
                                           links=[("homepage", "https://x.test")], profile_url="https://openstates.org/p")])
        for expected in ("Ashley Moody** (Republican)", "Senator, District FL, Senate, United States", "e@x.test",
                         "Capitol Office: 1 Main St", "[homepage](https://x.test)", "https://openstates.org/p", "Authoritative Source"):
            assert expected in text

    def test_several_current_matches_are_listed_and_the_model_is_told_to_ask(self):
        text = format_legislators([self._p(), self._p(name="Ashley Moody Jr", person_id="b", party="Democratic")], asked="Moody")
        assert "Several legislators match \"Moody\"" in text and "ask them" in text and "Ashley Moody Jr (Democratic)" in text

    def test_current_members_are_preferred_over_former_ones(self):
        text = format_legislators([self._p(current=False, name="Old Moody"), self._p()], asked="Moody")
        assert "Ashley Moody" in text and "Old Moody" not in text and "Several" not in text

    def test_a_former_legislator_alone_is_said_to_be_former(self):
        assert "a former legislator" in format_legislators([self._p(current=False, title="", chamber="")])

    def test_nothing_is_empty(self):
        assert format_legislators(None) == "" and format_legislators([]) == ""


def _agent(index=NEW_INDEX, found=None, by_id=None) -> VoteBotAgent:
    a = VoteBotAgent.__new__(VoteBotAgent)
    a.settings = SimpleNamespace(bill_filter_key="ocd_bill_id" if index == NEW_INDEX else "webflow_id")
    person = Legislator(PERSON_ID, "Ashley Moody", "Republican", "Senator", "Senate", "FL", "United States", True)
    a.legislators = SimpleNamespace(
        find_by_id=AsyncMock(return_value=by_id if by_id is not None else person),
        find_by_name=AsyncMock(return_value=found if found is not None else [person]),
    )
    return a


class TestAgentContext:
    async def test_a_legislator_page_with_an_openstates_id_looks_the_person_up_by_id(self):
        agent = _agent()
        text = await agent._legislator_context_from_api_v3(
            "What is her party?", PageContext(type="legislator", id=PERSON_ID, title="Ashley Moody"))
        assert "Ashley Moody" in text
        agent.legislators.find_by_id.assert_awaited_once_with(PERSON_ID)
        agent.legislators.find_by_name.assert_not_called()

    async def test_a_legislator_page_without_an_id_uses_its_title_and_jurisdiction(self):
        agent = _agent()
        await agent._legislator_context_from_api_v3(
            "How do I contact them?", PageContext(type="legislator", id="123", title="Ashley Moody", jurisdiction="US"))
        agent.legislators.find_by_name.assert_awaited_once_with("Ashley Moody", "US")

    async def test_a_legislator_page_with_neither_looks_nothing_up(self):
        agent = _agent()
        assert await agent._legislator_context_from_api_v3("hi", PageContext(type="legislator", id="123")) == ""
        agent.legislators.find_by_name.assert_not_called()

    async def test_a_name_on_a_bill_page_is_looked_up(self):
        agent = _agent()
        assert "Ashley Moody" in await agent._legislator_context_from_api_v3(
            "How did Ashley Moody vote?", PageContext(type="bill", id="HR 1"))
        agent.legislators.find_by_name.assert_awaited_once_with("Ashley Moody", None)

    @pytest.mark.parametrize("message", ["Who is Senator Ashley Moody?", "Contact Representative Smith Jones please",
                                         "tell me about the congressman Rick Scott"])
    async def test_a_cue_word_makes_a_name_on_a_general_page_worth_a_lookup(self, message):
        agent = _agent()
        await agent._legislator_context_from_api_v3(message, PageContext(type="general"))
        agent.legislators.find_by_name.assert_awaited_once()

    @pytest.mark.parametrize("message", ["What is the Florida education budget?", "Summarize HB 363 for me"])
    async def test_an_ordinary_question_on_a_general_page_is_not_sent_to_api_v3(self, message):
        agent = _agent()
        assert await agent._legislator_context_from_api_v3(message, PageContext(type="general")) == ""
        agent.legislators.find_by_name.assert_not_called()

    async def test_no_match_or_a_failure_is_empty_not_an_error(self):
        agent = _agent(found=[])
        assert await agent._legislator_context_from_api_v3("Senator Nobody", PageContext(type="bill", id="HB 1")) == ""
        agent.legislators.find_by_name = AsyncMock(return_value=None)
        assert await agent._legislator_context_from_api_v3("Senator Nobody", PageContext(type="bill", id="HB 1")) == ""

    async def test_a_slow_api_v3_costs_the_context_not_the_answer(self, monkeypatch):
        monkeypatch.setattr("votebot.core.agent.BUDGET_SECONDS", 0.05)
        agent = _agent()

        async def slow(*a, **k):
            await asyncio.sleep(5)

        agent.legislators.find_by_name = slow
        assert await agent._legislator_context_from_api_v3("Senator Moody", PageContext(type="bill", id="HB 1")) == ""

    def test_cue_words(self):
        for text in ("Sen. Smith", "the Representative", "Rep. Jones", "Congresswoman X", "lawmakers", "Senators from FL"):
            assert LEGISLATOR_CUES.search(text), text
        for text in ("What is the Florida education budget?", "the repository", "a repetition", "present"):
            assert not LEGISLATOR_CUES.search(text), text

    def test_the_name_guess_is_shared_with_the_legacy_lookup(self):
        assert VoteBotAgent._candidate_person_name("How did Ashley Moody vote on it?") == "Ashley Moody"
        assert VoteBotAgent._candidate_person_name("how did she vote?") is None


class TestReachesThePrompt:
    """The real streaming method: the live legislator context ends up in the system prompt, and the
    legacy index keeps its old lookup."""

    @staticmethod
    def _stream_agent(canonical: bool):
        from tests.unit.test_agent_stream_votes_flag import _agent as stream_agent

        agent = stream_agent("", should_use_tool=False)
        prompts = []

        async def stream(**kwargs):
            prompts.append(kwargs["system_prompt"])
            yield legislators_stream_chunk("Done.", done=True)

        agent.llm = SimpleNamespace(stream=stream)
        if not canonical:
            agent.settings = Settings(_env_file=None)  # legacy index
        return agent, prompts

    async def _ask(self, agent, page):
        return [c async for c in agent.process_message_stream(message="How did Ashley Moody vote?", session_id="s", page_context=page)]

    async def test_canonical_index_puts_the_api_v3_profile_in_the_prompt(self):
        agent, prompts = self._stream_agent(canonical=True)
        agent.legislators = _agent().legislators
        await self._ask(agent, PageContext(type="bill", id="HR 1"))
        assert "Legislator Profile" in prompts[0] and "Ashley Moody" in prompts[0]

    async def test_legacy_index_keeps_the_old_lookup_and_never_touches_api_v3_people_service(self):
        agent, prompts = self._stream_agent(canonical=False)
        agent._prefetch_legislator_info = AsyncMock(return_value="LEGACY LEGISLATOR INFO")
        agent._legislator_context_from_api_v3 = AsyncMock(side_effect=AssertionError("must not be called"))
        await self._ask(agent, PageContext(type="bill", id="HR 1"))
        assert "LEGACY LEGISLATOR INFO" in prompts[0]


def legislators_stream_chunk(text, done=False):
    from votebot.services.llm import StreamChunk

    return StreamChunk(text=text, done=done)
