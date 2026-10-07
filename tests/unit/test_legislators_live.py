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
from votebot.services.legislators import (
    NO_MATCH,
    UNAVAILABLE,
    Legislator,
    LegislatorLookupService,
    PeopleMatch,
    format_legislators,
)

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

        match = await _service(monkeypatch, handler).find_by_name("Ashley Moody", "us")
        (person,) = match.people
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

        person = (await _service(monkeypatch, handler).find_by_id(PERSON_ID)).people[0]
        assert person.person_id == PERSON_ID and seen[0].url.params["id"] == PERSON_ID

    async def test_a_former_legislator_has_no_current_role(self, monkeypatch):
        raw = _raw()
        raw["current_role"] = None
        match = await _service(monkeypatch, lambda r: httpx.Response(200, json={"results": [raw]})).find_by_name("X")
        (person,) = match.people
        assert person.current is False and person.title == ""

    @pytest.mark.parametrize("answer", [httpx.Response(500), httpx.Response(200, content=b"<html>"),
                                        httpx.Response(200, json=[1]), httpx.Response(200, json={"results": "x"})])
    async def test_anything_unusable_is_none(self, monkeypatch, answer):
        service = _service(monkeypatch, lambda r: answer)
        assert await service.find_by_name("Ashley Moody") is None
        assert await service.find_by_id(PERSON_ID) is None

    async def test_unusable_rows_are_skipped_and_an_unreachable_service_is_none(self, monkeypatch):
        rows = [_raw(), {"id": "x"}, "junk", {"name": "No id"}]
        match = await _service(monkeypatch, lambda r: httpx.Response(200, json={"results": rows})).find_by_name("X")
        assert [p.name for p in match.people] == ["Ashley Moody"]

        def down(request):
            raise httpx.ConnectError("down")

        assert await _service(monkeypatch, down).find_by_name("X") is None


class TestMatchTotals:
    async def test_the_total_comes_from_api_v3s_pagination_and_defaults_to_what_was_returned(self, monkeypatch):
        body = {"results": [_raw(), _raw(name="Other", id="ocd-person/b")], "pagination": {"total_items": 37, "per_page": 10}}
        match = await _service(monkeypatch, lambda r: httpx.Response(200, json=body)).find_by_name("Moody")
        assert (len(match.people), match.total) == (2, 37)
        match = await _service(monkeypatch, lambda r: httpx.Response(200, json={"results": [_raw()]})).find_by_name("Moody")
        assert (len(match.people), match.total) == (1, 1)

    async def test_nobody_matching_is_an_empty_match_not_a_failure(self, monkeypatch):
        match = await _service(monkeypatch, lambda r: httpx.Response(200, json={"results": []})).find_by_name("Zzz")
        assert match is not None and match.people == [] and match.total == 0


class TestFormatting:
    def _p(self, **kw):
        base = {"person_id": PERSON_ID, "name": "Ashley Moody", "party": "Republican", "title": "Senator", "chamber": "Senate",
                "district": "FL", "jurisdiction": "United States", "current": True}
        return Legislator(**{**base, **kw})

    def test_one_match_is_a_full_profile(self):
        text = format_legislators(PeopleMatch([self._p(email="e@x.test", offices=["Capitol Office: 1 Main St"],
                                           links=[("homepage", "https://x.test")], profile_url="https://openstates.org/p")], 1))
        for expected in ("Ashley Moody** (Republican)", "Senator, District FL, Senate, United States", "e@x.test",
                         "Capitol Office: 1 Main St", "[homepage](https://x.test)", "https://openstates.org/p", "Authoritative Source"):
            assert expected in text

    def test_several_current_matches_are_listed_and_the_model_is_told_to_ask(self):
        text = format_legislators(PeopleMatch([self._p(), self._p(name="Ashley Moody Jr", person_id="b", party="Democratic")], 2), asked="Moody")
        assert "Several legislators match \"Moody\"" in text and "ask them" in text and "Ashley Moody Jr (Democratic)" in text

    def test_current_members_are_preferred_over_former_ones(self):
        text = format_legislators(PeopleMatch([self._p(current=False, name="Old Moody"), self._p()], 2), asked="Moody")
        assert "Ashley Moody" in text and "Old Moody" not in text and "Several" not in text

    def test_a_former_legislator_alone_is_said_to_be_former(self):
        assert "a former legislator" in format_legislators(PeopleMatch([self._p(current=False, title="", chamber="")], 1))

    def test_nothing_is_empty(self):
        assert format_legislators(None) == "" and format_legislators(PeopleMatch()) == ""

    def test_a_match_larger_than_what_was_returned_says_so(self):
        text = format_legislators(PeopleMatch([self._p(), self._p(person_id="b", name="Other Moody")], 37), asked="Moody")
        assert "37 people match in all" in text and "ask for a fuller name" in text

    def test_one_person_returned_out_of_many_is_not_presented_as_the_only_match(self):
        text = format_legislators(PeopleMatch([self._p()], 12), asked="Moody")
        assert "Several legislators match" in text and "12 people match in all" in text


def _agent(index=NEW_INDEX, found=None, by_id=None) -> VoteBotAgent:
    a = VoteBotAgent.__new__(VoteBotAgent)
    a.settings = SimpleNamespace(bill_filter_key="ocd_bill_id" if index == NEW_INDEX else "webflow_id")
    person = Legislator(PERSON_ID, "Ashley Moody", "Republican", "Senator", "Senate", "FL", "United States", True)
    a.legislators = SimpleNamespace(
        find_by_id=AsyncMock(return_value=by_id if by_id is not None else PeopleMatch([person], 1)),
        find_by_name=AsyncMock(return_value=found if found is not None else PeopleMatch([person], 1)),
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

    async def test_nobody_matching_says_so_and_a_failure_tells_the_model_it_could_not_look(self):
        bill = PageContext(type="bill", id="HB 1")
        agent = _agent(found=PeopleMatch())
        none = await agent._legislator_context_from_api_v3("Senator Nobody", bill)
        assert none == NO_MATCH.format(asked="Nobody") and "from memory" in none  # told not to supply a role or district
        agent.legislators.find_by_name = AsyncMock(return_value=None)  # api-v3 failed
        text = await agent._legislator_context_from_api_v3("Senator Nobody", bill)
        assert text == UNAVAILABLE and "from memory" in text
        agent.legislators.find_by_id = AsyncMock(return_value=None)
        page = PageContext(type="legislator", id=PERSON_ID)
        assert await agent._legislator_context_from_api_v3("hi", page) == UNAVAILABLE

    @pytest.mark.parametrize("message", [
        "Who is Nancy Pelosi and which district does she represent?",
        "tell me about Ashley Moody",
        "Who is Moody?",  # opens by asking who someone is: one word is enough
        "How did Ashley Moody vote?",
    ])
    async def test_a_named_person_on_a_general_page_is_looked_up_with_or_without_a_cue_word(self, message):
        # VOTEBOT-24: these were answered from the model's memory ("Who is Nancy Pelosi?" gave the district from training data)
        agent = _agent()
        text = await agent._legislator_context_from_api_v3(message, PageContext(type="general"))
        agent.legislators.find_by_name.assert_awaited_once()
        assert "Ashley Moody" in text  # the fake returns this person: the live profile reached the context

    async def test_a_misspelled_name_finds_nobody_and_the_model_is_told_not_to_fill_it_in_from_memory(self):
        # api-v3 matches substrings, not misspellings: "Nancy Pelsoi" matches nobody
        agent = _agent(found=PeopleMatch())
        text = await agent._legislator_context_from_api_v3("Who is Nancy Pelsoi?", PageContext(type="general"))
        assert 'No legislator record matched "Nancy Pelsoi"' in text and "from memory" in text

    async def test_a_name_typed_in_lower_case_after_who_is_is_still_looked_up(self):
        agent = _agent()
        await agent._legislator_context_from_api_v3(
            "who is nancy pelosi and which district does she represent", PageContext(type="general"))
        agent.legislators.find_by_name.assert_awaited_once_with("Nancy Pelosi", None)
        agent2 = _agent()
        await agent2._legislator_context_from_api_v3("who's pelosi's district?", PageContext(type="general"))
        agent2.legislators.find_by_name.assert_awaited_once_with("Pelosi", None)

    async def test_lower_case_without_who_is_is_not_taken_for_a_name(self):
        agent = _agent()
        assert await agent._legislator_context_from_api_v3("how did ashley moody vote?", PageContext(type="general")) == ""
        agent.legislators.find_by_name.assert_not_called()

    async def test_one_bare_word_names_a_person_only_when_exactly_one_person_matches(self):
        # "Tell me about Jordan" may be a country, a company or a surname shared by many: never guess among them
        general = PageContext(type="general")
        many = PeopleMatch([Legislator(f"ocd-person/{n}", f"Jordan {n}", "Republican", "Representative", "House", "OH", "US", True)
                            for n in range(3)], 3)
        assert await _agent(found=many)._legislator_context_from_api_v3("Tell me about Jordan", general) == ""
        assert await _agent(found=PeopleMatch())._legislator_context_from_api_v3("Tell me about Jordan", general) == ""
        failing = _agent()
        failing.legislators.find_by_name = AsyncMock(return_value=None)
        assert await failing._legislator_context_from_api_v3("Tell me about Jordan", general) == ""  # no claim either way
        assert "Ashley Moody" in await _agent()._legislator_context_from_api_v3("Tell me about Moody", general)

    async def test_a_cue_word_makes_one_word_enough_as_before(self):
        agent = _agent(found=PeopleMatch())
        assert "No legislator record matched" in await agent._legislator_context_from_api_v3(
            "Who is Senator Nobody?", PageContext(type="general"))

    async def test_a_two_word_name_that_is_not_a_person_gets_only_a_hedged_note(self):
        agent = _agent(found=PeopleMatch())
        text = await agent._legislator_context_from_api_v3("Tell me about Planned Parenthood", PageContext(type="general"))
        assert 'matched "Planned Parenthood"' in text and "Ignore this if the user is not asking about a legislator" in text
        assert "nobody by that name" not in text  # it must not claim more than the substring match established

    @pytest.mark.parametrize("message", ["Who is AARP?", "Tell me about Florida", "Who is the governor?", "What is Medicaid"])
    async def test_questions_with_no_possible_name_are_not_sent_to_api_v3(self, message):
        agent = _agent()
        assert await agent._legislator_context_from_api_v3(message, PageContext(type="general")) == ""
        agent.legislators.find_by_name.assert_not_called()

    async def test_a_slow_api_v3_costs_the_profile_and_the_model_is_told(self, monkeypatch):
        import time

        monkeypatch.setattr("votebot.core.agent._enrichment_budget_left", lambda: 0.05)
        agent = _agent()
        person = Legislator(PERSON_ID, "Ashley Moody", "Republican", "Senator", "Senate", "FL", "United States", True)

        async def slow(*a, **k):
            await asyncio.sleep(3)
            return PeopleMatch([person], 1)  # VALID data, late: without the timeout this would be the profile

        agent.legislators.find_by_name = slow
        started = time.monotonic()
        text = await agent._legislator_context_from_api_v3("Senator Moody", PageContext(type="bill", id="HB 1"))
        assert text == UNAVAILABLE and time.monotonic() - started < 1.5

    def test_cue_words(self):
        for text in ("Sen. Smith", "the Representative", "Rep. Jones", "Congresswoman X", "lawmakers", "Senators from FL"):
            assert LEGISLATOR_CUES.search(text), text
        for text in ("What is the Florida education budget?", "the repository", "a repetition", "present"):
            assert not LEGISLATOR_CUES.search(text), text

    def test_the_name_guess_is_shared_with_the_legacy_lookup(self):
        assert VoteBotAgent._candidate_person_name("How did Ashley Moody vote on it?") == "Ashley Moody"
        assert VoteBotAgent._candidate_person_name("how did she vote?") is None


class TestNameGuard:
    """A guess at a name costs a live call, so ordinary bill-page messages must not make one."""

    @pytest.mark.parametrize("message", [
        "Summarize this bill", "Why was it amended in Florida?", "Explain HB 363 to me", "What does AARP think of it?",
        "What is the status?", "Is Texas considering this too?", "Does Congress have to pass this?",
    ])
    def test_ordinary_messages_yield_no_name(self, message):
        assert VoteBotAgent._legislator_name_in(message, require_cue=False) is None

    @pytest.mark.parametrize(("message", "expected"), [
        ("How did Ashley Moody vote?", "Ashley Moody"),
        ("Did Rick Scott support it?", "Rick Scott"),
        ("Tell Ashley Moody what you think", "Ashley Moody"),
        ("Who is Senator Moody?", "Moody"),
        ("What did Rep. Smith say?", "Smith"),
    ])
    def test_names_are_found(self, message, expected):
        assert VoteBotAgent._legislator_name_in(message, require_cue=False) == expected

    def test_other_pages_need_a_cue_even_for_a_full_name(self):
        assert VoteBotAgent._legislator_name_in("How did Ashley Moody vote?", require_cue=True) is None
        assert VoteBotAgent._legislator_name_in("How did Senator Ashley Moody vote?", require_cue=True) == "Ashley Moody"

    async def test_an_ordinary_bill_page_message_makes_no_live_call(self):
        agent = _agent()
        for message in ("Summarize this bill", "Why was it amended in Florida?"):
            assert await agent._legislator_context_from_api_v3(message, PageContext(type="bill", id="HB 1")) == ""
        agent.legislators.find_by_name.assert_not_called()


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

    async def _ask(self, agent, page, message="How did Ashley Moody vote?"):
        return [c async for c in agent.process_message_stream(message=message, session_id="s", page_context=page)]

    async def test_a_named_person_on_a_general_page_puts_the_profile_in_the_prompt(self):
        # VOTEBOT-24: "Who is Nancy Pelosi...?" on a general page used to reach the model with no live record at all
        agent, prompts = self._stream_agent(canonical=True)
        agent.legislators = _agent().legislators
        await self._ask(agent, PageContext(type="general"), "Who is Nancy Pelosi and which district does she represent?")
        assert "Legislator Profile" in prompts[0] and "Ashley Moody" in prompts[0]
        agent.legislators.find_by_name.assert_awaited_once()

    async def test_an_unavailable_api_v3_on_a_general_page_tells_the_model_it_could_not_look(self):
        agent, prompts = self._stream_agent(canonical=True)
        agent.legislators = _agent().legislators
        agent.legislators.find_by_name = AsyncMock(return_value=None)
        await self._ask(agent, PageContext(type="general"), "Who is Nancy Pelosi?")
        assert UNAVAILABLE in prompts[0]

    async def test_a_misspelled_name_on_a_general_page_tells_the_model_not_to_answer_from_memory(self):
        agent, prompts = self._stream_agent(canonical=True)
        agent.legislators = _agent(found=PeopleMatch()).legislators
        await self._ask(agent, PageContext(type="general"), "Who is Nancy Pelsoi?")
        assert 'No legislator record matched "Nancy Pelsoi"' in prompts[0]

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
