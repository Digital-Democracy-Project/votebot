"""VOTEBOT-16: `bill_votes_tool_used` on the streaming path, from the agent to the websocket frame.

The smoke test relies on this flag to prove a vote question was answered by the live OpenStates
lookup (votes are not in the new index), so it is tested against the REAL agent method: only the
lookups and the model stream are stubbed. A flag that is never set, or never passed on, fails here.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from votebot.api.routes import websocket as ws
from votebot.api.schemas.chat import PageContext
from votebot.config import Settings
from votebot.core.agent import VoteBotAgent
from votebot.core.retrieval import RetrievalResult
from votebot.services.llm import StreamChunk

BILL = "a3f7c0d1-1111-4222-8333-444455556666"
PAGE = PageContext(type="bill", id="HB 1", jurisdiction="FL", session="2026", ocd_bill_id=BILL)


def _agent(prefetch_returns: str, should_use_tool: bool = True) -> VoteBotAgent:
    agent = VoteBotAgent.__new__(VoteBotAgent)
    agent.settings = Settings(pinecone_index_name="ddp-knowledge-base", _env_file=None)
    agent.retrieval = SimpleNamespace(
        retrieve=AsyncMock(return_value=RetrievalResult(chunks=[], query_used="q", filters_applied={}, total_retrieved=0))
    )

    async def stream(**kwargs):
        yield StreamChunk(text="It passed ")
        yield StreamChunk(text="80 to 30.", done=True)

    agent.llm = SimpleNamespace(stream=stream)
    agent._should_use_bill_votes_tool = lambda *a, **k: should_use_tool
    agent._prefetch_bill_info = AsyncMock(return_value=prefetch_returns)
    agent._prefetch_legislator_info = AsyncMock(return_value="")
    agent._log_query = lambda **kwargs: None  # the JSONL logger is not under test
    return agent


async def _final_chunk(agent: VoteBotAgent):
    chunks = [
        c async for c in agent.process_message_stream(message="How did the vote go?", session_id="s1", page_context=PAGE)
    ]
    done = [c for c in chunks if c.done]
    assert len(done) == 1
    return done[0]


class TestFlagOnTheFinalChunk:
    async def test_true_when_the_live_lookup_returned_data(self):
        agent = _agent("## Bill Info (OpenStates)\nHouse vote 80-30")
        final = await _final_chunk(agent)
        assert final.metadata.bill_votes_tool_used is True
        agent._prefetch_bill_info.assert_awaited_once()

    async def test_false_when_the_lookup_found_nothing(self):
        # The tool counts as used only if live data came back, not merely because it was tried.
        agent = _agent("")
        final = await _final_chunk(agent)
        assert final.metadata.bill_votes_tool_used is False
        agent._prefetch_bill_info.assert_awaited_once()

    async def test_false_and_no_lookup_when_the_tool_is_not_triggered(self):
        agent = _agent("never used", should_use_tool=False)
        final = await _final_chunk(agent)
        assert final.metadata.bill_votes_tool_used is False
        agent._prefetch_bill_info.assert_not_called()


class TestFlagOnTheWebsocketFrame:
    """The same real agent behind /ws/chat: the flag must reach `stream_end`."""

    def _client(self, monkeypatch, prefetch_returns: str) -> TestClient:
        ws.sessions.clear()
        monkeypatch.setattr(ws, "VoteBotAgent", lambda: _agent(prefetch_returns))
        monkeypatch.setattr(ws, "get_slack_service", lambda: SimpleNamespace(is_configured=False))
        monkeypatch.setattr(ws, "get_redis_store", lambda: SimpleNamespace(is_available=False))
        monkeypatch.setattr(ws, "manager", ws.ConnectionManager())
        app = FastAPI()
        app.include_router(ws.router)
        return TestClient(app)

    def _stream_end(self, client: TestClient) -> dict:
        with client.websocket_connect("/ws/chat?session_id=s1") as conn:
            conn.receive_json()
            conn.send_json({"type": "user_message", "payload": {"message": "How did the vote go?", "page_context": {
                "type": "bill", "id": "HB 1", "jurisdiction": "FL", "session": "2026", "ocd_bill_id": BILL}}})
            while True:
                frame = conn.receive_json()
                if frame["type"] == "stream_end":
                    return frame["payload"]

    @pytest.mark.parametrize(("prefetch", "expected"), [("live data", True), ("", False)])
    def test_stream_end_reports_what_the_agent_did(self, monkeypatch, prefetch, expected):
        assert self._stream_end(self._client(monkeypatch, prefetch))["bill_votes_tool_used"] is expected
