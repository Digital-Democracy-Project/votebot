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

from tests.unit.test_websocket_protocol import BILL, DEFAULT_CITATIONS, FakeAgent
from votebot.api.routes import websocket as ws

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "smoke_ws.py"
spec = importlib.util.spec_from_file_location("smoke_ws", SCRIPT)
smoke = importlib.util.module_from_spec(spec)
sys.modules["smoke_ws"] = smoke  # dataclasses look their module up here
spec.loader.exec_module(smoke)

OTHER = "b4a8d1e2-2222-4333-8444-555566667777"


@pytest.fixture
def server(monkeypatch):
    FakeAgent.calls, FakeAgent.fail, FakeAgent.citations = [], False, DEFAULT_CITATIONS
    FakeAgent.votes_tool_used, FakeAgent.responder = False, None
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
        FakeAgent.citations = [DEFAULT_CITATIONS[0].model_copy(update={"document_id": f"bill-text:{OTHER}:12-chunk-0"})]
        assert _run(tmp_path, server, [_case()]) == 1
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

    def test_an_unreachable_instance_fails_instead_of_crashing(self, tmp_path, capsys):
        assert _run(tmp_path, "ws://127.0.0.1:1/ws/chat", [_case()]) == 1
        assert "FAIL  FL HB 1" in capsys.readouterr().out

    def test_one_failing_case_does_not_hide_a_passing_one(self, server, tmp_path, capsys):
        failing = _case(name="bad", questions=[{"message": "q", "expect_any": ["nope"]}])
        assert _run(tmp_path, server, [_case(), failing]) == 1
        out = capsys.readouterr().out
        assert "PASS  FL HB 1" in out and "FAIL  bad" in out and "1/2 cases passed" in out


def _good_responder(message: str):
    """What a healthy new-index VoteBot would say to the shipped questions."""
    if "vote" in message:
        return ["The House passed it 80 to 30."], True
    if "version" in message:
        return ["I am reading the Engrossed text dated 2026-03-04."], False
    return ["The bill does X."], False


class TestShippedCases:
    def _patch_discovery(self, monkeypatch):
        async def discover(jurisdiction):
            return {"type": "bill", "id": "HB 1", "jurisdiction": jurisdiction, "session": "2026", "ocd_bill_id": BILL}

        monkeypatch.setattr(smoke, "discover_bill", discover)

    def test_five_jurisdictions_each_with_a_discovered_bill_and_a_vote_question(self):
        cases = json.loads((SCRIPT.parent / "smoke_cases.json").read_text())
        assert [c["discover"]["jurisdiction"] for c in cases] == ["FL", "WA", "US", "VA", "MI"]
        for case in cases:
            votes = [q for q in case["questions"] if q.get("expect_votes_tool")]
            assert len(votes) == 1 and "vote" in votes[0]["message"]
            # citations are required of the case as a whole, never of one answer: whether the model writes a
            # citation in a single answer is its choice (the first gate 3 run failed on exactly that)
            assert case["min_cited_answers"] == 1
            assert all("min_citations" not in q and "expect_regex" not in q for q in case["questions"])
            assert all("what changed" not in q["message"].lower() for q in case["questions"])
            assert "bill-votes" not in case["expect_types"] and "bill-version-diff" not in case["expect_types"]

    def test_a_healthy_answer_set_passes_all_five(self, server, tmp_path, monkeypatch, capsys):
        self._patch_discovery(monkeypatch)
        FakeAgent.responder = _good_responder
        cases = json.loads((SCRIPT.parent / "smoke_cases.json").read_text())
        assert _run(tmp_path, server, cases) == 0
        out = capsys.readouterr().out
        assert "5/5 cases passed" in out and "discovered HB 1" in out
        assert "NOT proven" not in out and "judged from citations only" in out  # no --retrieval: say so

    def test_a_vote_answered_without_the_live_tool_fails_on_the_new_index_only(self, server, tmp_path, monkeypatch, capsys):
        self._patch_discovery(monkeypatch)
        FakeAgent.responder = lambda m: (_good_responder(m)[0], False)  # retrieval answered the vote question
        cases = json.loads((SCRIPT.parent / "smoke_cases.json").read_text())[:1]
        assert _run(tmp_path, server, cases) == 1
        assert "bill_votes_tool_used is False, expected True" in capsys.readouterr().out
        assert _run(tmp_path, server, cases, "--index", "legacy") == 0  # either path may answer there


class TestCitationsAndTimeouts:
    def test_min_citations_fails_a_turn_that_cites_nothing(self, server, tmp_path, capsys):
        FakeAgent.citations = []
        case = _case(questions=[{"message": "What does this bill do?", "min_citations": 1}])
        assert _run(tmp_path, server, [case]) == 1
        assert "0 citations, expected at least 1" in capsys.readouterr().out

    def test_min_cited_answers_passes_when_one_of_several_answers_cites_the_bill(self, server, tmp_path, capsys):
        FakeAgent.responder = None
        calls = {"n": 0}
        original = smoke.ask

        async def ask_once_cited(*a, **k):
            turn = await original(*a, **k)
            calls["n"] += 1
            if calls["n"] != 1:
                turn.citations = []
            return turn

        smoke.ask, restore = ask_once_cited, original
        try:
            case = _case(questions=[{"message": "What does this bill do?"}, {"message": "And the sponsors?"}])
            case["min_cited_answers"] = 1
            assert _run(tmp_path, server, [case]) == 0
        finally:
            smoke.ask = restore

    def test_min_cited_answers_fails_when_no_answer_cites_the_bill(self, server, tmp_path, capsys):
        FakeAgent.citations = []
        case = _case(questions=[{"message": "What does this bill do?"}, {"message": "And the sponsors?"}])
        case["min_cited_answers"] = 1
        assert _run(tmp_path, server, [case]) == 1
        assert "0 answers cited this bill in each of 3 attempts, expected at least 1" in capsys.readouterr().out

    def test_a_case_that_fails_only_for_lack_of_a_citation_is_run_again_and_can_pass(self, server, tmp_path, capsys):
        FakeAgent.citations = []
        original = smoke.ask
        calls = {"n": 0}

        async def cited_on_second_attempt(*a, **k):
            turn = await original(*a, **k)
            calls["n"] += 1
            if calls["n"] >= 2:
                turn.citations = [{"document_id": f"{BILL}-v1", "source": "x"}]
            return turn

        smoke.ask = cited_on_second_attempt
        try:
            case = _case()
            case["min_cited_answers"] = 1
            assert _run(tmp_path, server, [case]) == 0
        finally:
            smoke.ask = original
        assert "attempt 1: no answer cited this bill" in capsys.readouterr().out

    def test_naming_the_bill_is_not_evidence_and_attempts_are_bounded(self, server, tmp_path, capsys):
        FakeAgent.citations = []
        FakeAgent.responder = lambda m: (["Here is what HB 1 does: the bill does X."], False)  # the page context supplies the number
        case = _case()
        case["min_cited_answers"] = 1
        assert _run(tmp_path, server, [case], "--citation-attempts", "2") == 1
        out = capsys.readouterr().out
        assert "0 answers cited this bill in each of 2 attempts" in out
        assert len(FakeAgent.calls) == 2  # one question, two attempts

    def test_another_problem_ends_the_case_without_a_retry(self, server, tmp_path):
        FakeAgent.citations = []
        FakeAgent.responder = lambda m: (["The bill does X."], False)
        case = _case(questions=[{"message": "What does this bill do?", "expect_any": ["will never appear"]}])
        case["min_cited_answers"] = 1
        assert _run(tmp_path, server, [case]) == 1
        assert len(FakeAgent.calls) == 1

    def test_a_cited_answer_whose_citations_carry_no_bill_id_is_not_isolation_evidence(self, server, tmp_path, capsys):
        FakeAgent.citations = [DEFAULT_CITATIONS[0].model_copy(update={"document_id": "https://flsenate.gov/x"})]
        case = _case(questions=[{"message": "What does this bill do?", "min_citations": 1}])
        assert _run(tmp_path, server, [case]) == 1
        assert "no citation carries this bill's id" in capsys.readouterr().out

    def test_the_report_says_how_many_citations_carried_a_bill_id(self, server, tmp_path, capsys):
        assert _run(tmp_path, server, [_case()]) == 0
        assert "1/1 citations carry a bill id" in capsys.readouterr().out

    def test_the_target_is_printed(self, server, tmp_path, capsys):
        _run(tmp_path, server, [_case()])
        assert f"target {server}" in capsys.readouterr().out

    def test_a_turn_that_stalls_is_a_failed_case_not_a_hang(self, server, tmp_path, capsys, monkeypatch):
        async def stall(*a, **k):
            await asyncio.sleep(30)

        monkeypatch.setattr(smoke, "ask", stall)
        assert _run(tmp_path, server, [_case()], "--turn-timeout", "0.2") == 1
        assert "TimeoutError" in capsys.readouterr().out


class TestCheckTurn:
    def _turn(self, **kw):
        base = {"frames": ["stream_start", "stream_chunk", "stream_end"], "answer": "ok", "citations": [], "confidence": 0.8}
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
        self._patch(monkeypatch, [self._chunk("bill-text"), self._chunk("organization")])
        problems, notes = self._check(("bill-text", "organization"))
        assert problems == [] and any("matches" in n for n in notes)

    def test_a_chunk_of_another_bill_fails_isolation(self, monkeypatch):
        self._patch(monkeypatch, [self._chunk("bill-text"), self._chunk("bill-text", bill=OTHER)])
        assert any("other bills" in p for p in self._check()[0])

    def test_a_missing_document_type_fails(self, monkeypatch):
        self._patch(monkeypatch, [self._chunk("bill-text")])
        assert any("no organization chunk" in p for p in self._check(("bill-text", "organization"))[0])

    def test_a_document_id_that_is_not_the_current_versions_fails(self, monkeypatch):
        self._patch(monkeypatch, [self._chunk("bill-text", document_id="10")], current="11")
        assert any("!= current version 11" in p for p in self._check()[0])

    def test_no_chunks_fails(self, monkeypatch):
        self._patch(monkeypatch, [])
        assert self._check()[0] == ["retrieval returned no chunks"]


class TestIndexAbsenceAndDiscovery:
    class _Store:
        def __init__(self, held=(), found=None, has_bill_text=True):
            self.held, self.found, self.has_bill_text, self.filters = set(held), found, has_bill_text, []

        async def query(self, query, top_k=10, filter=None, include_metadata=True):
            self.filters.append(filter)
            if filter.get("document_type") == "bill-text":
                if self.found is not None:
                    return [SimpleNamespace(metadata=self.found)]
                return [SimpleNamespace(metadata={})] if self.has_bill_text else []
            return [SimpleNamespace(metadata={})] if filter.get("document_type") in self.held else []

    def _patch(self, monkeypatch, store):
        from votebot.config import Settings

        monkeypatch.setattr("votebot.services.vector_store.VectorStoreService", lambda settings: store)
        monkeypatch.setattr("votebot.config.get_settings", lambda: Settings(pinecone_index_name="ddp-knowledge-base", _env_file=None))

    def test_an_index_without_votes_or_diffs_passes(self, monkeypatch):
        store = self._Store()
        self._patch(monkeypatch, store)
        assert asyncio.run(smoke.forbidden_types_check(["bill-votes", "bill-version-diff"])) == []
        assert [f["document_type"] for f in store.filters] == ["bill-text", "bill-votes", "bill-version-diff"]

    def test_an_empty_result_does_not_count_as_absent_when_the_query_path_finds_nothing(self, monkeypatch):
        # A wrong index, namespace or filter key would also return nothing for bill-votes.
        store = self._Store(has_bill_text=False)
        self._patch(monkeypatch, store)
        problems = asyncio.run(smoke.forbidden_types_check(["bill-votes", "bill-version-diff"]))
        assert len(problems) == 1 and "positive control failed" in problems[0]
        assert [f["document_type"] for f in store.filters] == ["bill-text"]  # stopped before the forbidden queries

    def test_vote_documents_in_the_index_fail(self, monkeypatch):
        self._patch(monkeypatch, self._Store(held={"bill-votes"}))
        problems = asyncio.run(smoke.forbidden_types_check(["bill-votes", "bill-version-diff"]))
        assert len(problems) == 1 and "bill-votes" in problems[0]

    def test_main_adds_the_absence_check_only_with_retrieval_on_the_canonical_index(self, server, tmp_path, monkeypatch, capsys):
        self._patch(monkeypatch, self._Store(held={"bill-votes"}))
        path = tmp_path / "none.json"
        path.write_text("[]")
        run = lambda *extra: asyncio.run(smoke.main(["--url", server, "--cases", str(path), *extra]))  # noqa: E731
        assert run() == 0
        assert run("--retrieval") == 1 and "the index holds bill-votes documents" in capsys.readouterr().out
        assert run("--retrieval", "--index", "legacy") == 0

    def test_discovery_picks_an_embedded_bill_for_the_jurisdiction(self, monkeypatch):
        found = {"ocd_bill_id": BILL, "gov_id": "HB 7", "session_code": "2025-2026"}
        store = self._Store(found=found)
        self._patch(monkeypatch, store)
        context = asyncio.run(smoke.discover_bill("wa"))
        assert context == {"type": "bill", "id": "HB 7", "jurisdiction": "WA", "session": "2025-2026", "ocd_bill_id": BILL}
        assert store.filters == [{"document_type": "bill-text", "jurisdiction": "WA"}]

    def test_discovery_fails_clearly_when_nothing_is_embedded(self, monkeypatch):
        self._patch(monkeypatch, self._Store(found={}))
        with pytest.raises(RuntimeError, match="no embedded bill-text found for MI"):
            asyncio.run(smoke.discover_bill("MI"))
