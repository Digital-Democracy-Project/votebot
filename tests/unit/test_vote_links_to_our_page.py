"""VOTEBOT-23: live bill data never hands the model OpenStates' web address when our own site is configured.

A live check (notes reply 32) showed an answer citing https://openstates.org/fl/bills/2024/HB7089/, taken from
the "More info" line of the live bill-info context. Answers link to OUR pages (`DDP_SITE_BASE_URL`, VOTEBOT-15).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from votebot.api.schemas.chat import PageContext
from votebot.config import Settings
from votebot.core.agent import VoteBotAgent
from votebot.core.prompts import CITATION_INSTRUCTION, ENHANCED_CITATION_INSTRUCTION
from votebot.core.retrieval import RetrievalResult
from votebot.services.bill_votes import BillInfoResult, BillVotesService

SITE = "https://site.test"
OPENSTATES = "https://openstates.org/fl/bills/2024/HB7089/"
PAGE_URL = f"{SITE}/explore/FL/2024/HB%207089"
PAGE = PageContext(type="bill", id="HB 7089", jurisdiction="FL", session="2024")


def _result(identifier="HB 7089", jurisdiction="fl") -> BillInfoResult:
    return BillInfoResult(
        bill_id="ocd-bill/x", bill_identifier=identifier, jurisdiction=jurisdiction, title="Transparency",
        description=None, session="2024", status=None, chamber="house", sponsors=[], actions=[], votes=[],
        openstates_url=OPENSTATES,
    )


def _service(site: str = SITE) -> BillVotesService:
    return BillVotesService(Settings(ddp_site_base_url=site, _env_file=None))


class TestTheContextTheModelIsGiven:
    def test_our_page_is_the_link_and_openstates_is_not_in_the_context(self):
        text = _service().format_bill_info_document(_result(), page_url=PAGE_URL)
        assert f"**Bill page:** {PAGE_URL}" in text and "openstates.org" not in text

    def test_no_known_page_means_no_link_at_all_not_openstates(self):
        text = _service().format_bill_info_document(_result())
        assert "openstates.org" not in text and "Bill page" not in text and "More info" not in text

    def test_without_our_site_configured_nothing_changes(self):
        text = _service(site="").format_bill_info_document(_result(), page_url=PAGE_URL)
        assert f"**More info:** {OPENSTATES}" in text and "Bill page" not in text

    def test_the_citation_instructions_do_not_model_an_openstates_link(self):
        assert "openstates.org" not in CITATION_INSTRUCTION and "openstates.org" not in ENHANCED_CITATION_INSTRUCTION


def _agent(site: str = SITE) -> VoteBotAgent:
    agent = VoteBotAgent.__new__(VoteBotAgent)
    agent.settings = Settings(ddp_site_base_url=site, _env_file=None)
    return agent


class TestOurPageForTheBillTheVisitorIsOn:
    def test_the_same_bill_gets_our_page(self):
        assert _agent()._own_page_url(PAGE, _result()) == PAGE_URL
        assert _agent()._own_page_url(PAGE, _result(identifier="HB7089", jurisdiction="FL")) == PAGE_URL

    def test_the_same_identifier_in_another_session_gets_no_link(self):
        # "HB 1" is reused every session; our URL carries the page's session, so a result of another one must not get it
        other_session = _result()
        other_session.session = "2023"
        assert _agent()._own_page_url(PAGE, other_session) is None
        assert _agent()._own_page_url(PageContext(type="bill", id="HB 7089", jurisdiction="FL"), _result()) is None

    def test_a_us_bill_matches_across_case_and_spacing(self):
        us_page = PageContext(type="bill", id="HR 6855", jurisdiction="US", session="118")
        us = _result(identifier="hr6855", jurisdiction="us")
        us.session = "118"
        assert _agent()._own_page_url(us_page, us) == f"{SITE}/explore/US/118/HR%206855"

    def test_another_bill_gets_no_link_rather_than_a_guessed_one(self):
        assert _agent()._own_page_url(PAGE, _result(identifier="HB 100")) is None
        assert _agent()._own_page_url(PAGE, _result(jurisdiction="va")) is None

    def test_a_general_page_or_an_unset_site_gets_none(self):
        assert _agent()._own_page_url(PageContext(type="general"), _result()) is None
        assert _agent(site="")._own_page_url(PAGE, _result()) is None


class TestTheStreamingPrefetchPassesIt:
    async def test_the_prefetched_context_has_our_page_and_no_openstates_link(self):
        agent = _agent()
        agent.bill_votes = _service()
        agent.bill_votes.get_bill_info = AsyncMock(return_value=_result())
        context = await agent._prefetch_bill_info("How did the vote on this bill go?", PAGE)
        assert PAGE_URL in context and "openstates.org" not in context

    async def test_a_bill_other_than_the_page_s_gets_no_link(self):
        agent = _agent()
        agent.bill_votes = _service()
        agent.bill_votes.get_bill_info = AsyncMock(return_value=_result(identifier="HB 100"))
        context = await agent._prefetch_bill_info("How did the vote on HB 100 go?", PAGE)
        assert "openstates.org" not in context and PAGE_URL not in context


class TestTheNonStreamingToolPath:
    """`services/llm.py` runs the same lookup for the REST (non-streaming) agent path; it must link the same way."""

    async def _tool(self, page_url_for, site=SITE):
        from votebot.services.llm import LLMService

        service = LLMService.__new__(LLMService)
        bill_votes = _service(site)
        bill_votes.get_bill_info = AsyncMock(return_value=_result())
        text, tool = await service._execute_bill_info_tool(
            {"jurisdiction": "fl", "session": "2024", "bill_identifier": "HB 7089"}, bill_votes, page_url_for
        )
        return text

    async def test_it_links_our_page_and_not_openstates(self):
        text = await self._tool(lambda result: PAGE_URL)
        assert f"**Bill page:** {PAGE_URL}" in text and "openstates.org" not in text

    async def test_without_a_page_there_is_no_link_at_all(self):
        text = await self._tool(None)
        assert "openstates.org" not in text and "Bill page" not in text

    async def test_with_the_site_unset_it_keeps_the_openstates_line(self):
        assert f"**More info:** {OPENSTATES}" in await self._tool(None, site="")

    async def test_the_agent_hands_the_tool_path_its_own_page_lookup(self):
        from types import SimpleNamespace

        agent = _agent()
        seen = {}

        async def complete(**kwargs):
            seen["fn"] = kwargs.get("page_url_for")
            from votebot.services.llm import LLMResponse

            return LLMResponse(content="ok", tokens_used=1, model="fake", web_citations=[])

        agent.llm = SimpleNamespace(complete=complete)
        agent.bill_votes = _service()
        agent.retrieval = SimpleNamespace(
            retrieve=AsyncMock(return_value=RetrievalResult(chunks=[], query_used="q", filters_applied={}, total_retrieved=0)),
        )
        agent._should_use_bill_votes_tool = lambda *a, **k: True
        agent._perform_web_search = AsyncMock(return_value=[])
        agent._log_query = lambda **kwargs: None
        agent.legislators = SimpleNamespace(find_by_id=AsyncMock(return_value=None), find_by_name=AsyncMock(return_value=None))
        await agent.process_message(message="How did the vote go?", session_id="s", page_context=PAGE)
        assert seen["fn"](_result()) == PAGE_URL and seen["fn"](_result(identifier="HB 100")) is None
