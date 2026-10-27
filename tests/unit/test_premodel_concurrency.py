"""VOTEBOT-47: retrieval and the lookups that do not read its result run at the same time (streaming path).

The real `process_message_stream` runs; only retrieval, the lookups and the model stream are stubbed, so the order the
contexts reach the prompt and the time before the model is asked are the real ones.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from votebot.api.schemas.chat import PageContext
from votebot.config import Settings
from votebot.core.agent import VoteBotAgent, _enrichment_deadline
from votebot.core.retrieval import RetrievalResult
from votebot.services.llm import StreamChunk

BILL = "a3f7c0d1-1111-4222-8333-444455556666"
PAGE = PageContext(type="bill", id="HB 1", jurisdiction="FL", session="2026", ocd_bill_id=BILL)
LOOKUPS = ("bill_info", "legislator", "vote", "webflow", "org", "broker")


class Recorder:
    """Which stubs ran when, and what the model was told."""

    def __init__(self):
        self.started: dict[str, float] = {}
        self.ended: dict[str, float] = {}
        self.deadlines: dict[str, float | None] = {}
        self.prompt = ""
        self.timings = None
        self.cancelled: list[str] = []


def _agent(rec: Recorder, delay: float = 0.0, retrieval_delay: float | None = None, **overrides) -> VoteBotAgent:
    loop = asyncio.get_running_loop()
    agent = VoteBotAgent.__new__(VoteBotAgent)
    agent.settings = Settings(pinecone_index_name="ddp-knowledge-base", _env_file=None)

    def lookup(name, text, fail=False, wait=delay):
        async def run(*a, **k):
            rec.started[name] = loop.time()
            rec.deadlines[name] = _enrichment_deadline.get()
            try:
                await asyncio.sleep(wait)
                if fail:
                    raise RuntimeError(f"{name} down")
                return text
            except asyncio.CancelledError:
                rec.cancelled.append(name)
                raise
            finally:
                rec.ended[name] = loop.time()

        return run

    async def retrieve(**kwargs):
        rec.started["retrieval"] = loop.time()
        try:
            await asyncio.sleep(delay if retrieval_delay is None else retrieval_delay)
            if overrides.get("retrieval_fails"):
                raise RuntimeError("pinecone down")
        finally:
            rec.ended["retrieval"] = loop.time()
        return RetrievalResult(chunks=[], query_used="q", filters_applied={}, total_retrieved=0)

    async def prefetch_bill_info(*a, **k):
        return await lookup("bill_info", "BILLINFO-MARK")(*a, **k)

    async def stream(**kwargs):
        rec.prompt = kwargs["system_prompt"]
        yield StreamChunk(text="ok")
        yield StreamChunk(text="", done=True)

    agent.retrieval = SimpleNamespace(retrieve=retrieve)
    agent.llm = SimpleNamespace(stream=stream)
    agent._should_use_bill_votes_tool = overrides.get("trigger", lambda *a, **k: True)
    agent._prefetch_bill_info = prefetch_bill_info
    agent._legislator_context_from_api_v3 = lookup("legislator", "LEGISLATOR-MARK", fail=overrides.get("legislator_fails", False))
    agent._is_dispute_or_correction = lambda m: True
    agent._verify_legislator_vote = lookup("vote", "VOTEVERIFY-MARK")
    agent._verify_from_webflow = lookup("webflow", "WEBFLOW-MARK")
    agent._is_org_position_query = lambda m: True
    agent._prefetch_bill_org_positions = lookup("org", "ORGPOSITIONS-MARK")
    agent._wants_bill_broker_data = lambda page: True
    agent._prefetch_bill_broker_data = lookup("broker", "BROKER-MARK")
    agent._log_query = lambda **kwargs: setattr(rec, "timings", kwargs["timings"])
    return agent


async def _ask(agent: VoteBotAgent):
    return [c async for c in agent.process_message_stream(message="How did the vote go?", session_id="s", page_context=PAGE)]


class TestTheWaitBeforeTheModel:
    async def test_it_is_the_slowest_lookup_not_the_sum(self):
        rec = Recorder()
        agent = _agent(rec, delay=0.1)  # retrieval and six lookups of 0.1 s: 0.7 s one after another
        await _ask(agent)
        assert rec.timings.get_mark("model_call_start") < 300  # ms: about 100 here, 700 one after another

    async def test_every_lookup_starts_before_retrieval_ends(self):
        rec = Recorder()
        await _ask(_agent(rec, delay=0.05))
        assert set(LOOKUPS) <= set(rec.started)
        assert max(rec.started[n] for n in LOOKUPS) < rec.ended["retrieval"]

    async def test_the_lookups_share_the_messages_one_deadline(self):
        rec = Recorder()
        await _ask(_agent(rec))
        deadlines = {rec.deadlines[n] for n in LOOKUPS}
        assert len(deadlines) == 1 and None not in deadlines  # one budget for the message, not one per task


class TestTheContextIsAsBefore:
    async def test_the_order_the_model_reads_it_in_is_unchanged(self):
        rec = Recorder()
        await _ask(_agent(rec, delay=0.01))
        order = ["WEBFLOW-MARK", "ORGPOSITIONS-MARK", "VOTEVERIFY-MARK", "BILLINFO-MARK", "LEGISLATOR-MARK", "BROKER-MARK"]
        positions = [rec.prompt.index(m) for m in order]
        assert positions == sorted(positions)  # most authoritative first; the analysis last, after the sources

    async def test_the_phases_are_still_timed_and_overlap(self):
        rec = Recorder()
        await _ask(_agent(rec, delay=0.05))
        phases = rec.timings.phases_ms
        assert {"retrieval", "bill_info_prefetch", "legislator_lookup", "vote_verification", "org_positions",
                "webflow_verification", "bill_broker_data"} <= set(phases)
        assert sum(phases.values()) > rec.timings.get_mark("model_call_start")  # durations overlap


class TestFailures:
    async def test_a_lookup_that_fails_costs_only_its_own_context(self):
        rec = Recorder()
        chunks = await _ask(_agent(rec, legislator_fails=True))
        assert chunks and any(c.done for c in chunks)  # the answer still streams
        assert "LEGISLATOR-MARK" not in rec.prompt
        assert all(m in rec.prompt for m in ("BILLINFO-MARK", "VOTEVERIFY-MARK", "ORGPOSITIONS-MARK", "WEBFLOW-MARK", "BROKER-MARK"))

    async def test_a_failing_retrieval_cancels_the_lookups_still_running(self):
        rec = Recorder()
        agent = _agent(rec, delay=0.5, retrieval_delay=0.01, retrieval_fails=True)
        with pytest.raises(RuntimeError, match="pinecone down"):
            await _ask(agent)
        assert sorted(rec.cancelled) == sorted(LOOKUPS)  # waited for: every one has ended when the error surfaces
        assert not (asyncio.all_tasks() - {asyncio.current_task()})

    async def test_giving_up_cancels_the_lookups_too(self):
        rec = Recorder()
        agent = _agent(rec, delay=0.5)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(_ask(agent), 0.05)
        assert sorted(rec.cancelled) == sorted(LOOKUPS)
        assert not (asyncio.all_tasks() - {asyncio.current_task()})


class TestTheBillInfoTrigger:
    async def test_a_trigger_that_holds_at_full_confidence_starts_with_retrieval(self):
        rec = Recorder()
        await _ask(_agent(rec, delay=0.05))
        assert rec.started["bill_info"] < rec.ended["retrieval"]

    async def test_a_trigger_that_needs_a_low_confidence_runs_after_retrieval_as_before(self):
        rec = Recorder()
        # says "no" at full confidence, "yes" for the empty retrieval's 0.0: it cannot be started early
        agent = _agent(rec, delay=0.05, trigger=lambda conf, *a, **k: conf < 0.5)
        await _ask(agent)
        assert rec.started["bill_info"] >= rec.ended["retrieval"]
        assert "BILLINFO-MARK" in rec.prompt

    async def test_a_trigger_that_is_off_runs_no_lookup(self):
        rec = Recorder()
        await _ask(_agent(rec, trigger=lambda *a, **k: False))
        assert "bill_info" not in rec.started and "BILLINFO-MARK" not in rec.prompt

    @pytest.mark.parametrize(
        "message",
        [
            "How did the vote go?", "Who supported it?", "What is the status of this bill?", "tell me about HB 1234",
            "Explain HB 12", "What does SB 5 do?", "What does this bill do?", "hello", "Who are the sponsors?",
            "What is the latest action?", "summary please",
        ],
    )
    def test_a_yes_at_full_confidence_is_a_yes_at_every_confidence(self, message):
        """The property the early start relies on: a lower confidence only ever adds reasons to look the bill up."""
        agent = VoteBotAgent.__new__(VoteBotAgent)
        agent.settings = Settings(pinecone_index_name="ddp-knowledge-base", bill_votes_tool_enabled=True, _env_file=None)
        for page in (PAGE, PageContext(type="general")):
            if agent._should_use_bill_votes_tool(1.0, message, page, None):
                assert all(agent._should_use_bill_votes_tool(c, message, page, None) for c in (0.0, 0.2, 0.49, 0.5, 0.9))
