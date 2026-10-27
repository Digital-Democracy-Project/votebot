"""Phase timings and time to first token (VOTEBOT latency work): measured per message, logged on `query_processed`.

`PhaseTimings` is tested alone; then against the REAL agent method with only the lookups and the model stream
stubbed, so a phase that is not wrapped, or a first-token mark that is not set, fails here.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from votebot.api.schemas.chat import PageContext
from votebot.config import Settings
from votebot.core.agent import VoteBotAgent
from votebot.core.retrieval import RetrievalResult
from votebot.services.llm import StreamChunk
from votebot.services.query_logger import QueryLogger
from votebot.utils.timings import PhaseTimings

BILL = "a3f7c0d1-1111-4222-8333-444455556666"
PAGE = PageContext(type="bill", id="HB 1", jurisdiction="FL", session="2026", ocd_bill_id=BILL)


class TestPhaseTimings:
    async def test_a_phase_records_its_duration(self):
        t = PhaseTimings()
        with t.phase("work"):
            await asyncio.sleep(0.05)
        assert 40 <= t.phases_ms["work"] < 500

    async def test_a_phase_entered_twice_is_summed(self):
        t = PhaseTimings()
        for _ in range(2):
            with t.phase("work"):
                await asyncio.sleep(0.03)
        assert t.phases_ms["work"] >= 50

    def test_a_phase_that_raises_is_still_recorded(self):
        t = PhaseTimings()
        with pytest.raises(RuntimeError), t.phase("boom"):
            raise RuntimeError
        assert "boom" in t.phases_ms

    async def test_a_mark_is_measured_from_the_start_and_the_first_call_wins(self):
        t = PhaseTimings()
        await asyncio.sleep(0.03)
        t.mark("seen")
        first = t.get_mark("seen")
        await asyncio.sleep(0.03)
        t.mark("seen")
        assert first >= 25
        assert t.get_mark("seen") == first

    def test_an_unset_mark_is_none(self):
        assert PhaseTimings().get_mark("never") is None


def _agent(*, retrieval_delay: float = 0.0, first_chunk_delay: float = 0.0, tool: bool = False) -> VoteBotAgent:
    agent = VoteBotAgent.__new__(VoteBotAgent)
    agent.settings = Settings(pinecone_index_name="ddp-knowledge-base", _env_file=None)

    async def retrieve(**kwargs):
        await asyncio.sleep(retrieval_delay)
        return RetrievalResult(chunks=[], query_used="q", filters_applied={}, total_retrieved=0)

    agent.retrieval = SimpleNamespace(retrieve=retrieve)

    async def stream(**kwargs):
        await asyncio.sleep(first_chunk_delay)
        yield StreamChunk(text="It passed ")
        yield StreamChunk(text="80 to 30.")
        yield StreamChunk(text="", done=True)  # as LLMService.stream ends: an empty done marker

    agent.llm = SimpleNamespace(stream=stream)
    agent._should_use_bill_votes_tool = lambda *a, **k: tool
    agent._prefetch_bill_info = AsyncMock(return_value="")
    agent._legislator_context_from_api_v3 = AsyncMock(return_value="")
    agent.logged = []
    agent._log_query = lambda **kwargs: agent.logged.append(kwargs)
    return agent


async def _run(agent: VoteBotAgent) -> list:
    return [
        c async for c in agent.process_message_stream(message="How did the vote go?", session_id="s1", page_context=PAGE)
    ]


class TestStreamingTimings:
    async def test_the_log_gets_the_phases_and_a_first_token_after_the_model_was_called(self):
        agent = _agent(retrieval_delay=0.06, first_chunk_delay=0.05)
        await _run(agent)

        (logged,) = agent.logged
        t = logged["timings"]
        assert t.phases_ms["retrieval"] >= 50
        assert "legislator_lookup" in t.phases_ms
        assert "bill_info_prefetch" not in t.phases_ms  # the tool was not triggered, so no phase is invented for it
        assert t.get_mark("model_call_start") >= t.phases_ms["retrieval"]
        # the model's own wait is in the first token, not in the pre-model time
        assert t.get_mark("first_token") >= t.get_mark("model_call_start") + 40

    async def test_the_bill_prefetch_phase_is_there_when_the_tool_ran(self):
        agent = _agent(tool=True)
        await _run(agent)
        assert "bill_info_prefetch" in agent.logged[0]["timings"].phases_ms

    async def test_first_token_is_the_first_text_not_the_done_marker(self):
        agent = _agent()
        chunks = await _run(agent)
        assert [c.text for c in chunks if not c.done] == ["It passed ", "80 to 30."]
        assert agent.logged[0]["timings"].get_mark("first_token") is not None

    async def test_a_saved_button_answer_has_a_first_token_too(self):
        agent = _agent()
        agent._maybe_serve_from_button_cache = AsyncMock(
            return_value={"response": "saved", "citations": [], "cache_hit_metadata": {}}
        )
        await _run(agent)
        t = agent.logged[0]["timings"]
        assert "button_cache" in t.phases_ms
        assert t.get_mark("first_token") is not None
        assert "retrieval" not in t.phases_ms


class TestQueryLogFields:
    async def test_the_timings_are_written_on_the_event(self, tmp_path):
        logger = QueryLogger(log_dir=str(tmp_path))
        await logger.log_event(
            event_type="query_processed",
            session_id="s1",
            duration_ms=4200,
            first_token_ms=1800,
            phase_ms={"retrieval": 1100, "bill_info_prefetch": 2100},
            marks_ms={"model_call_start": 1500, "first_token": 1800},
        )
        (line,) = next(tmp_path.glob("*.jsonl")).read_text().splitlines()
        entry = json.loads(line)
        assert entry["first_token_ms"] == 1800
        assert entry["phase_ms"] == {"retrieval": 1100, "bill_info_prefetch": 2100}
        assert entry["marks_ms"]["model_call_start"] == 1500

    async def test_an_event_without_timings_has_no_such_keys(self, tmp_path):
        logger = QueryLogger(log_dir=str(tmp_path))
        await logger.log_event(event_type="message_received", session_id="s1")
        entry = json.loads(next(tmp_path.glob("*.jsonl")).read_text())
        assert not {"first_token_ms", "phase_ms", "marks_ms"} & entry.keys()
