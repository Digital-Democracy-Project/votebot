"""VOTEBOT-16: scripts/smoke_ws.py, checked against a real local WebSocket server.

A uvicorn server in a thread serves the real /ws/chat route with the agent faked (same fake as
test_websocket_protocol), so the script's client, frame handling, checks and exit status run for
real without OpenAI, Pinecone or any credentials.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import FastAPI

from tests.unit.test_websocket_protocol import BILL, FakeAgent
from votebot.api.routes import websocket as ws

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "smoke_ws.py"
spec = importlib.util.spec_from_file_location("smoke_ws", SCRIPT)
smoke = importlib.util.module_from_spec(spec)
sys.modules["smoke_ws"] = smoke  # dataclasses look their module up here
spec.loader.exec_module(smoke)

OTHER = "b4a8d1e2-2222-4333-8444-555566667777"


@pytest.fixture
def server(monkeypatch):
    FakeAgent.calls, FakeAgent.fail = [], False
    ws.sessions.clear()
    monkeypatch.setattr(ws, "VoteBotAgent", FakeAgent)
    monkeypatch.setattr(ws, "get_slack_service", lambda: SimpleNamespace(is_configured=False))
    monkeypatch.setattr(ws, "get_redis_store", lambda: SimpleNamespace(is_available=False))
    monkeypatch.setattr(ws, "manager", ws.ConnectionManager())
    app = FastAPI()
    app.include_router(ws.router)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    for _ in range(100):
        if instance.started:
            break
        time.sleep(0.05)
    yield f"ws://127.0.0.1:{port}/ws/chat"
    instance.should_exit = True
    thread.join(timeout=5)


def _run(tmp_path, url, cases, *extra) -> int:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(cases))
    return asyncio.run(smoke.main(["--url", url, "--cases", str(path), "--timeout", "5", *extra]))


def _case(**overrides):
    case = {
        "name": "FL HB 1",
        "page_context": {"type": "bill", "id": "HB 1", "jurisdiction": "FL", "session": "2026", "ocd_bill_id": BILL},
        "questions": [{"message": "What does this bill do?", "expect_any": ["does X"]}],
    }
    return {**case, **overrides}


class TestAgainstALocalServer:
    def test_a_good_answer_passes(self, server, tmp_path, capsys):
        assert _run(tmp_path, server, [_case()]) == 0
        assert "PASS  FL HB 1" in capsys.readouterr().out
        # the page context reached the agent the way the widget sends it
        assert FakeAgent.calls[0]["page_context"].ocd_bill_id == BILL

    def test_a_citation_of_another_bill_fails_isolation(self, server, tmp_path, capsys):
        FakeAgent.citations = [FakeAgent.citations[0].model_copy(update={"document_id": f"bill-text:{OTHER}:12-chunk-0"})]
        try:
            assert _run(tmp_path, server, [_case()]) == 1
        finally:
            FakeAgent.citations = [FakeAgent.citations[0].model_copy(update={"document_id": f"bill-text:{BILL}:11"})]
        assert "belongs to another bill" in capsys.readouterr().out

    def test_an_answer_missing_the_expected_text_fails(self, server, tmp_path, capsys):
        case = _case(questions=[{"message": "Who voted?", "expect_any": ["Yea"]}])
        assert _run(tmp_path, server, [case]) == 1
        assert "mentions none of ['Yea']" in capsys.readouterr().out

    def test_a_banned_phrase_fails(self, server, tmp_path):
        case = _case(questions=[{"message": "Who voted?", "expect_none": ["does X"]}])
        assert _run(tmp_path, server, [case]) == 1

    def test_an_agent_error_fails_the_case(self, server, tmp_path, capsys):
        FakeAgent.fail = True
        assert _run(tmp_path, server, [_case()]) == 1
        assert "processing_error" in capsys.readouterr().out

    def test_a_placeholder_bill_id_is_refused(self, server, tmp_path, capsys):
        case = _case()
        case["page_context"]["ocd_bill_id"] = smoke.PLACEHOLDER_BILL
        assert _run(tmp_path, server, [case]) == 1
        assert "placeholder" in capsys.readouterr().out

    def test_the_shipped_example_cases_all_need_a_real_bill_id(self, server, tmp_path):
        cases = json.loads((SCRIPT.parent / "smoke_cases.json").read_text())
        assert [c["page_context"]["jurisdiction"] for c in cases] == ["FL", "WA", "US", "VA", "MI"]
        assert _run(tmp_path, server, cases) == 1  # placeholders: not a pass by accident

    def test_an_unreachable_instance_fails_instead_of_crashing(self, tmp_path, capsys):
        assert _run(tmp_path, "ws://127.0.0.1:1/ws/chat", [_case()]) == 1
        assert "FAIL  FL HB 1" in capsys.readouterr().out

    def test_one_failing_case_does_not_hide_a_passing_one(self, server, tmp_path, capsys):
        failing = _case(name="bad", questions=[{"message": "q", "expect_any": ["nope"]}])
        assert _run(tmp_path, server, [_case(), failing]) == 1
        out = capsys.readouterr().out
        assert "PASS  FL HB 1" in out and "FAIL  bad" in out and "1/2 cases passed" in out


class TestCheckTurn:
    def _turn(self, **kw):
        base = dict(frames=["stream_start", "stream_chunk", "stream_end"], answer="ok", citations=[], confidence=0.8)
        return smoke.Turn(**{**base, **kw})

    def test_low_confidence_fails(self):
        assert smoke.check_turn(self._turn(confidence=0.2), {}, BILL, 0.5, True)

    def test_legacy_index_skips_the_citation_isolation_check(self):
        turn = self._turn(citations=[{"document_id": f"bill-text:{OTHER}:1"}])
        assert smoke.check_turn(turn, {}, BILL, 0.5, canonical=False) == []
        assert smoke.check_turn(turn, {}, BILL, 0.5, canonical=True)

    def test_organization_and_web_citations_are_not_mistaken_for_another_bill(self):
        turn = self._turn(citations=[{"document_id": "organization:42-chunk-0"}, {"document_id": "https://example.test/x"}])
        assert smoke.check_turn(turn, {}, BILL, 0.5, True) == []

    def test_wrong_frame_order_fails(self):
        assert smoke.check_turn(self._turn(frames=["stream_chunk", "stream_end"]), {}, BILL, 0.5, True)


class TestRetrievalChecks:
    """--retrieval reads chunks from the index; here the index is a fake."""

    @staticmethod
    def _chunk(doc_type, bill=BILL, document_id="11"):
        return SimpleNamespace(metadata={"document_type": doc_type, "ocd_bill_id": bill, "document_id": document_id})

    def _patch(self, monkeypatch, chunks, current="11"):
        from votebot.config import Settings

        class FakeRetrieval:
            def __init__(self, settings):
                pass

            async def retrieve(self, query, page_context):
                return SimpleNamespace(chunks=chunks, current_document_id=current)

        monkeypatch.setattr("votebot.core.retrieval.RetrievalService", FakeRetrieval)
        monkeypatch.setattr("votebot.config.get_settings", lambda: Settings(pinecone_index_name="ddp-knowledge-base", _env_file=None))

    def _check(self, expect=("bill-text",)):
        context = {"type": "bill", "id": "HB 1", "jurisdiction": "FL", "ocd_bill_id": BILL}
        return asyncio.run(smoke.retrieval_checks({}, context, list(expect)))

    def test_clean_retrieval_passes_and_reports_the_current_version(self, monkeypatch):
        self._patch(monkeypatch, [self._chunk("bill-text"), self._chunk("bill-votes")])
        problems, notes = self._check(("bill-text", "bill-votes"))
        assert problems == [] and any("matches" in n for n in notes)

    def test_a_chunk_of_another_bill_fails_isolation(self, monkeypatch):
        self._patch(monkeypatch, [self._chunk("bill-text"), self._chunk("bill-text", bill=OTHER)])
        assert any("other bills" in p for p in self._check()[0])

    def test_a_missing_document_type_fails(self, monkeypatch):
        self._patch(monkeypatch, [self._chunk("bill-text")])
        assert any("no bill-votes chunk" in p for p in self._check(("bill-text", "bill-votes"))[0])

    def test_a_document_id_that_is_not_the_current_versions_fails(self, monkeypatch):
        self._patch(monkeypatch, [self._chunk("bill-text", document_id="10")], current="11")
        assert any("!= current version 11" in p for p in self._check()[0])

    def test_no_chunks_fails(self, monkeypatch):
        self._patch(monkeypatch, [])
        assert self._check()[0] == ["retrieval returned no chunks"]
