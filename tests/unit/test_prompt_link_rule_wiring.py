"""VOTEBOT-23: the link rule for the new site reaches the model on BOTH agent paths, and only on the new index.

`test_prompts.py` tests `build_system_prompt` directly; this runs the real `process_message` and
`process_message_stream` with only the lookups and the model call stubbed, so a path that stops passing
the new-index flag (and so hands the model the old-site example links again) fails here.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.unit.test_agent_nonstreaming_enrichment import _agent as rest_agent
from tests.unit.test_ocd_bill_id_retrieval import NEW_INDEX
from votebot.api.schemas.chat import PageContext
from votebot.config import Settings
from votebot.core.agent import VoteBotAgent
from votebot.core.retrieval import RetrievalResult
from votebot.services.legislators import PeopleMatch
from votebot.services.llm import StreamChunk

BILL = "a3f7c0d1-1111-4222-8333-444455556666"
PAGE = PageContext(type="bill", id="HB 1", jurisdiction="FL", session="2026", ocd_bill_id=BILL)
RULE = "Never write any other bill URL and never build one from a bill's name or number"
OLD_EXAMPLE = "digitaldemocracyproject.org/bills/education-funding-act"


def _stream_agent(canonical: bool):
    agent = VoteBotAgent.__new__(VoteBotAgent)
    agent.settings = Settings(pinecone_index_name=NEW_INDEX if canonical else "votebot-large", _env_file=None)
    agent.retrieval = SimpleNamespace(
        retrieve=AsyncMock(return_value=RetrievalResult(chunks=[], query_used="q", filters_applied={}, total_retrieved=0))
    )
    prompts = []

    async def stream(**kwargs):
        prompts.append(kwargs["system_prompt"])
        yield StreamChunk(text="ok", done=True)

    agent.llm = SimpleNamespace(stream=stream)
    agent._should_use_bill_votes_tool = lambda *a, **k: False
    agent._prefetch_legislator_info = AsyncMock(return_value="")
    agent.legislators = SimpleNamespace(
        find_by_id=AsyncMock(return_value=PeopleMatch([], 0)), find_by_name=AsyncMock(return_value=PeopleMatch([], 0))
    )
    agent._log_query = lambda **kwargs: None
    return agent, prompts


class TestTheRuleReachesTheModel:
    async def test_streaming_path(self):
        for canonical in (True, False):
            agent, prompts = _stream_agent(canonical)
            _ = [c async for c in agent.process_message_stream(message="Tell me about this bill", session_id="s", page_context=PAGE)]
            assert (RULE in prompts[0]) is canonical
            assert (OLD_EXAMPLE in prompts[0]) is (not canonical)

    async def test_rest_path(self):
        for canonical in (True, False):
            agent, prompts = rest_agent(canonical)
            await agent.process_message(message="Tell me about this bill", session_id="s", page_context=PAGE)
            assert (RULE in prompts[0]) is canonical
            assert (OLD_EXAMPLE in prompts[0]) is (not canonical)
