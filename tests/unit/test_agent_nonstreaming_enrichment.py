"""VOTEBOT-15: the NON-streaming `process_message` (REST) path carries the same live enrichments.

The streaming tests do not cover it: removing the retrieval notes or the live legislator profile
from this path used to leave every test green. Here the real method runs with only the lookups and
the model call stubbed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.unit.test_ocd_bill_id_retrieval import NEW_INDEX
from votebot.api.schemas.chat import PageContext
from votebot.config import Settings
from votebot.core.agent import VoteBotAgent
from votebot.core.retrieval import DIFF_UNAVAILABLE_NOTE, RetrievalResult
from votebot.services.legislators import Legislator, PeopleMatch
from votebot.services.llm import LLMResponse

PERSON_ID = "ocd-person/11111111-2222-3333-4444-555555555555"
BILL = "a3f7c0d1-1111-4222-8333-444455556666"


def _agent(canonical: bool = True, notes=()):
    agent = VoteBotAgent.__new__(VoteBotAgent)
    agent.settings = Settings(pinecone_index_name=NEW_INDEX if canonical else "votebot-large", _env_file=None)
    agent.retrieval = SimpleNamespace(
        retrieve=AsyncMock(
            return_value=RetrievalResult(chunks=[], query_used="q", filters_applied={}, total_retrieved=0, notes=list(notes))
        )
    )
    prompts = []

    async def complete(**kwargs):
        prompts.append(kwargs["system_prompt"])
        return LLMResponse(content="ok", tokens_used=1, model="fake", web_citations=[])

    agent.llm = SimpleNamespace(complete=complete)
    person = Legislator(PERSON_ID, "Ashley Moody", "Republican", "Senator", "Senate", "FL", "United States", True)
    agent.legislators = SimpleNamespace(
        find_by_id=AsyncMock(return_value=PeopleMatch([person], 1)),
        find_by_name=AsyncMock(return_value=PeopleMatch([person], 1)),
    )
    agent._should_use_bill_votes_tool = lambda *a, **k: False
    agent._perform_web_search = AsyncMock(return_value=[])
    agent._log_query = lambda **kwargs: None
    return agent, prompts


async def _ask(agent, page, message="How did Ashley Moody vote?"):
    return await agent.process_message(message=message, session_id="s", page_context=page)


class TestNonStreamingPath:
    async def test_the_live_legislator_profile_reaches_the_prompt(self):
        agent, prompts = _agent()
        await _ask(agent, PageContext(type="bill", id="HR 1", ocd_bill_id=BILL))
        assert "Legislator Profile" in prompts[0] and "Ashley Moody" in prompts[0]
        agent.legislators.find_by_name.assert_awaited_once()

    async def test_the_retrieval_notes_reach_the_prompt(self):
        agent, prompts = _agent(notes=[DIFF_UNAVAILABLE_NOTE])
        await _ask(agent, PageContext(type="bill", id="HR 1", ocd_bill_id=BILL), "What changed in this bill?")
        assert DIFF_UNAVAILABLE_NOTE in prompts[0]

    async def test_a_legislator_page_is_answered_about_its_legislator(self):
        agent, prompts = _agent()
        await _ask(agent, PageContext(type="legislator", id=PERSON_ID, title="Ashley Moody"), "What party are they in?")
        agent.legislators.find_by_id.assert_awaited_once()
        assert "Republican" in prompts[0]

    async def test_an_ordinary_message_makes_no_live_people_call(self):
        agent, prompts = _agent()
        await _ask(agent, PageContext(type="bill", id="HR 1", ocd_bill_id=BILL), "Summarize this bill")
        agent.legislators.find_by_name.assert_not_called()
        assert "Legislator Profile" not in prompts[0]

    async def test_the_legacy_index_gets_neither(self):
        agent, prompts = _agent(canonical=False)
        await _ask(agent, PageContext(type="bill", id="HR 1", slug="a-bill"))
        agent.legislators.find_by_name.assert_not_called()
        assert "Legislator Profile" not in prompts[0]
