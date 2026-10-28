"""VOTEBOT-60 with VOTEBOT-47: a legislator's scorecard is fetched at the same time as retrieval and the other lookups.

The streaming path gathers its pre-model context in `_gather_premodel_context` so the wait before the model is the
slowest lookup, not the sum. The scorecard lookup must start there too, not after it (it was once added after it,
which put its whole wait back in front of the model).
"""

from __future__ import annotations

import asyncio

from votebot.api.schemas.chat import PageContext
from votebot.core.agent import VoteBotAgent

from tests.unit.test_premodel_concurrency import Recorder, _agent

PERSON = "ocd-person/8056c810-7170-5009-88c3-a670ab30164b"
LEGISLATOR_PAGE = PageContext(type="legislator", id=PERSON, title="Nancy Pelosi", jurisdiction="US")


def _with_scorecard(rec: Recorder, wait: float, delay: float, fails: bool = False) -> VoteBotAgent:
    loop = asyncio.get_running_loop()
    agent = _agent(rec, delay=delay)
    agent._wants_legislator_scorecard = lambda page: True

    async def scorecard(message, page_context):
        rec.started["scorecard"] = loop.time()
        try:
            await asyncio.sleep(wait)
            if fails:
                raise RuntimeError("broker down")
            return "SCORECARD-MARK"
        finally:
            rec.ended["scorecard"] = loop.time()

    agent._prefetch_legislator_scorecard = scorecard
    return agent


async def _ask(agent: VoteBotAgent):
    return [
        c
        async for c in agent.process_message_stream(
            message="How aligned is this legislator with voters?", session_id="s", page_context=LEGISLATOR_PAGE
        )
    ]


class TestTheScorecardRunsWithTheOtherLookups:
    async def test_it_starts_before_retrieval_ends(self):
        rec = Recorder()
        await _ask(_with_scorecard(rec, wait=0.05, delay=0.05))
        assert rec.started["scorecard"] < rec.ended["retrieval"]

    async def test_it_does_not_add_its_wait_in_front_of_the_model(self):
        rec = Recorder()
        # retrieval and the other lookups take 0.1 s; the scorecard also 0.1 s: together about 0.1 s, not 0.2 s
        await _ask(_with_scorecard(rec, wait=0.1, delay=0.1))
        assert rec.timings.get_mark("model_call_start") < 170  # ms: about 100 here, about 200 if it ran after the rest

    async def test_it_is_timed_as_its_own_phase(self):
        rec = Recorder()
        await _ask(_with_scorecard(rec, wait=0.05, delay=0.0))
        assert rec.timings.phases_ms["legislator_scorecard"] >= 45


class TestTheAnswerStillGetsIt:
    async def test_the_scorecard_reaches_the_prompt(self):
        rec = Recorder()
        await _ask(_with_scorecard(rec, wait=0.0, delay=0.0))
        assert "SCORECARD-MARK" in rec.prompt

    async def test_a_failing_scorecard_costs_only_its_own_context(self):
        rec = Recorder()
        await _ask(_with_scorecard(rec, wait=0.0, delay=0.0, fails=True))
        assert "SCORECARD-MARK" not in rec.prompt
        assert "BROKER-MARK" in rec.prompt  # the other lookups are untouched
