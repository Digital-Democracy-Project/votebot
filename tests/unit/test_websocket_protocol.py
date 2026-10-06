"""VOTEBOT-16: the /ws/chat protocol, exercised the way the widget uses it.

Nothing here touches OpenAI, Pinecone, Redis or Slack: the agent is replaced by a fake that yields
the same `StreamChunkData` shapes, and Slack/Redis are stubbed out. The unit under test is the
route: session handshake, streaming frames, `context_update`, `ping`, validation, and how the
page context reaches the agent.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from votebot.api.routes import websocket as ws
from votebot.api.schemas.chat import Citation, ResponseMetadata
from votebot.core.agent import StreamChunkData

BILL = "a3f7c0d1-1111-4222-8333-444455556666"


DEFAULT_CITATIONS = [
    Citation(
        source="OpenStates archive",
        document_id=f"bill-text:{BILL}:11",
        excerpt="Section 1 ...",
        url="https://example.test/hb1",
        relevance_score=0.9,
    )
]


class FakeAgent:
    """Stands in for VoteBotAgent: records what it was given and streams a canned answer."""

    calls: list[dict] = []
    answer = ["The bill ", "does X."]
    citations = DEFAULT_CITATIONS
    fail = False

    async def process_message_stream(self, **kwargs):
        FakeAgent.calls.append(kwargs)
        if FakeAgent.fail:
            raise RuntimeError("boom")
        for text in FakeAgent.answer:
            yield StreamChunkData(text=text)
        yield StreamChunkData(
            text="",
            done=True,
            citations=FakeAgent.citations,
            metadata=ResponseMetadata(model="fake", tokens_used=1, retrieval_count=3, latency_ms=1.0),
        )


@pytest.fixture
def client(monkeypatch):
    FakeAgent.calls = []
    FakeAgent.fail = False
    FakeAgent.citations = DEFAULT_CITATIONS
    ws.sessions.clear()
    monkeypatch.setattr(ws, "VoteBotAgent", FakeAgent)
    monkeypatch.setattr(ws, "get_slack_service", lambda: SimpleNamespace(is_configured=False))
    monkeypatch.setattr(ws, "get_redis_store", lambda: SimpleNamespace(is_available=False))
    monkeypatch.setattr(ws, "manager", ws.ConnectionManager())
    try:  # the JSONL query logger is not under test; keep it from writing files
        import votebot.services.query_logger as query_logger

        monkeypatch.setattr(query_logger, "get_query_logger", lambda: None)
    except ImportError:  # aiofiles not installed: the route already skips logging
        pass
    app = FastAPI()
    app.include_router(ws.router)
    return TestClient(app)


def _user_message(text: str, page_context: dict | None = None, **extra) -> dict:
    payload = {"message": text, **extra}
    if page_context is not None:
        payload["page_context"] = page_context
    return {"type": "user_message", "payload": payload}


def _read_until(conn, wanted: str) -> list[dict]:
    """Frames up to and including the first `wanted` one."""
    frames = []
    while True:
        frame = conn.receive_json()
        frames.append(frame)
        if frame["type"] == wanted:
            return frames


class TestHandshake:
    def test_a_new_connection_gets_session_info_and_the_given_id(self, client):
        with client.websocket_connect("/ws/chat?session_id=abc123def456") as conn:
            assert conn.receive_json() == {
                "type": "session_info",
                "payload": {"session_id": "abc123def456", "restored": False},
            }

    def test_a_session_id_is_generated_when_none_is_given(self, client):
        with client.websocket_connect("/ws/chat") as conn:
            info = conn.receive_json()
        assert info["type"] == "session_info" and len(info["payload"]["session_id"]) == 12

    def test_reconnecting_restores_the_history(self, client):
        with client.websocket_connect("/ws/chat?session_id=s1") as conn:
            conn.receive_json()
            conn.send_json(_user_message("hello"))
            _read_until(conn, "stream_end")
        with client.websocket_connect("/ws/chat?session_id=s1") as conn:
            info = conn.receive_json()
            restored = conn.receive_json()
        assert info["payload"]["restored"] is True
        assert restored["type"] == "session_restored"
        assert [(m["role"], m["content"]) for m in restored["payload"]["messages"]] == [
            ("user", "hello"),
            ("assistant", "The bill does X."),
        ]


class TestStreaming:
    def test_a_message_streams_start_chunks_and_an_end_with_citations_and_confidence(self, client):
        with client.websocket_connect("/ws/chat?session_id=s1") as conn:
            conn.receive_json()
            conn.send_json(_user_message("What does this bill do?"))
            frames = _read_until(conn, "stream_end")

        assert [f["type"] for f in frames] == ["stream_start", "stream_chunk", "stream_chunk", "stream_end"]
        assert "".join(f["payload"]["text"] for f in frames if f["type"] == "stream_chunk") == "The bill does X."
        end = frames[-1]["payload"]
        assert end["citations"] == [
            {
                "source": "OpenStates archive",
                "document_id": f"bill-text:{BILL}:11",
                "excerpt": "Section 1 ...",
                "url": "https://example.test/hb1",
                "relevance_score": 0.9,
            }
        ]
        assert 0.5 < end["confidence"] <= 1.0  # retrieved docs + a citation raise it above the base
        assert end["requires_human"] is False

    def test_an_agent_failure_is_reported_as_a_processing_error(self, client):
        FakeAgent.fail = True
        with client.websocket_connect("/ws/chat?session_id=s1") as conn:
            conn.receive_json()
            conn.send_json(_user_message("What does this bill do?"))
            frames = _read_until(conn, "error")
        assert [f["type"] for f in frames] == ["stream_start", "error"]
        assert frames[-1]["payload"]["code"] == "processing_error"


class TestValidationAndPing:
    @pytest.mark.parametrize("text", ["", "   ", "\n"])
    def test_an_empty_message_is_rejected_without_calling_the_agent(self, client, text):
        with client.websocket_connect("/ws/chat?session_id=s1") as conn:
            conn.receive_json()
            conn.send_json(_user_message(text))
            error = conn.receive_json()
        assert error == {"type": "error", "payload": {"code": "empty_message", "message": "Message cannot be empty"}}
        assert FakeAgent.calls == []

    def test_ping_gets_pong(self, client):
        with client.websocket_connect("/ws/chat?session_id=s1") as conn:
            conn.receive_json()
            conn.send_json({"type": "ping"})
            assert conn.receive_json() == {"type": "pong"}

    def test_an_unknown_message_type_is_ignored_and_the_connection_stays_up(self, client):
        with client.websocket_connect("/ws/chat?session_id=s1") as conn:
            conn.receive_json()
            conn.send_json({"type": "nonsense"})
            conn.send_json({"type": "ping"})
            assert conn.receive_json() == {"type": "pong"}


class TestPageContext:
    def test_a_ddp_next_bill_context_reaches_the_agent_intact(self, client):
        context = {
            "type": "bill",
            "id": "HB 219",
            "jurisdiction": "FL",
            "session": "2026",
            "ocd_bill_id": BILL,
            "url": "https://example.test/explore/FL/2026/HB%20219",
        }
        with client.websocket_connect("/ws/chat?session_id=s1") as conn:
            conn.receive_json()
            conn.send_json(_user_message("What does this bill do?", context))
            _read_until(conn, "stream_end")

        page = FakeAgent.calls[0]["page_context"]
        assert (page.type, page.id, page.jurisdiction, page.session, page.ocd_bill_id) == (
            "bill", "HB 219", "FL", "2026", BILL,
        )

    def test_session_code_is_accepted_for_the_session(self, client):
        with client.websocket_connect("/ws/chat?session_id=s1") as conn:
            conn.receive_json()
            conn.send_json(_user_message("hi", {"type": "bill", "session-code": "119"}))
            _read_until(conn, "stream_end")
        assert FakeAgent.calls[0]["page_context"].session == "119"

    def test_history_is_passed_to_the_agent_without_the_current_message(self, client):
        with client.websocket_connect("/ws/chat?session_id=s1") as conn:
            conn.receive_json()
            conn.send_json(_user_message("first"))
            _read_until(conn, "stream_end")
            conn.send_json(_user_message("second"))
            _read_until(conn, "stream_end")
        assert [m["content"] for m in FakeAgent.calls[1]["conversation_history"]] == ["first", "The bill does X."]


class TestContextUpdate:
    def test_a_context_update_injects_a_system_note_naming_the_new_page(self, client):
        with client.websocket_connect("/ws/chat?session_id=s1") as conn:
            conn.receive_json()
            conn.send_json({"type": "context_update", "payload": {"page_context": {"type": "bill", "title": "HB 7"}}})
            conn.send_json({"type": "ping"})  # a reply proves the update was handled before it
            assert conn.receive_json() == {"type": "pong"}

        history = ws.sessions["s1"]["messages"]
        assert history[-1]["role"] == "system"
        assert "new bill page: HB 7" in history[-1]["content"]
        assert ws.sessions["s1"]["page_context"] == {"type": "bill", "title": "HB 7"}

    def test_the_note_reaches_the_agent_on_the_next_message(self, client):
        with client.websocket_connect("/ws/chat?session_id=s1") as conn:
            conn.receive_json()
            conn.send_json({"type": "context_update", "payload": {"page_context": {"type": "bill", "title": "HB 7"}}})
            conn.send_json(_user_message("what is this?", {"type": "bill", "title": "HB 7"}))
            _read_until(conn, "stream_end")
        history = FakeAgent.calls[0]["conversation_history"]
        assert any(m["role"] == "system" and "HB 7" in m["content"] for m in history)

    def test_navigating_to_a_general_page_says_so(self, client):
        with client.websocket_connect("/ws/chat?session_id=s1") as conn:
            conn.receive_json()
            conn.send_json({"type": "context_update", "payload": {"page_context": {"type": "general"}}})
            conn.send_json({"type": "ping"})
            conn.receive_json()
        assert "general page" in ws.sessions["s1"]["messages"][-1]["content"]


class TestHandoffTrigger:
    def test_asking_for_a_human_sets_requires_human(self, client):
        with client.websocket_connect("/ws/chat?session_id=s1") as conn:
            conn.receive_json()
            conn.send_json(_user_message("I want to speak to a human"))
            frames = _read_until(conn, "stream_end")
        assert frames[-1]["payload"]["requires_human"] is True
