#!/usr/bin/env python3
"""Smoke test for VoteBot over the real /ws/chat WebSocket (VOTEBOT-16).

Opens the socket the way the chat widget does, sends `user_message` with a ddp-next shaped
`page_context` and checks the streamed answer, citations and confidence. Needs no Webflow
credentials and works against either index; point it at an instance with --url.

    # The SYNC-92 smoke test: one case per jurisdiction, from a cases file
    python scripts/smoke_ws.py --url ws://localhost:8000/ws/chat --cases scripts/smoke_cases.json

    # The same, plus chunk-level checks that read the index directly (needs the .env keys of
    # the target instance, so the local and target PINECONE_INDEX_NAME must agree)
    python scripts/smoke_ws.py --cases scripts/smoke_cases.json --retrieval

Checks, per case and question:
  * stream_start, one or more stream_chunk, stream_end, and no `error` frame
  * a non-empty answer and confidence >= --min-confidence
  * `expect_any` (at least one, case-insensitive) / `expect_none` strings in the answer
  * `min_citations` (default 0: the model only cites when it chooses to; set 1 on questions that
    must be grounded in the bill)
  * bill isolation: no citation's id carries another bill's id (canonical index)
  * with --retrieval: every chunk retrieved for the bill carries that bill's id, the expected
    document types are present, and `bill-text` chunks of the current version carry the
    `document_id` api-v3 calls current

Exit status is 0 only if every check passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import websockets

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
DEFAULT_QUESTION = {"message": "What does this bill do?"}
PLACEHOLDER_BILL = "00000000-0000-0000-0000-000000000000"


@dataclass
class Result:
    name: str
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass
class Turn:
    """What came back for one user message."""

    frames: list[str] = field(default_factory=list)
    answer: str = ""
    citations: list[dict] = field(default_factory=list)
    confidence: float | None = None
    error: dict | None = None


async def ask(url: str, session_id: str, message: str, page_context: dict, timeout: float) -> Turn:
    """One question over a fresh connection (a new connection per turn keeps cases independent
    of server-side session state, except through the session id)."""
    turn = Turn()
    async with websockets.connect(f"{url}?session_id={session_id}", open_timeout=timeout) as conn:
        await conn.send(json.dumps({"type": "user_message", "payload": {"message": message, "page_context": page_context}}))
        while True:
            frame = json.loads(await asyncio.wait_for(conn.recv(), timeout))
            kind = frame.get("type")
            if kind == "session_info":
                continue
            turn.frames.append(kind)
            payload = frame.get("payload") or {}
            if kind == "stream_chunk":
                turn.answer += payload.get("text", "")
            elif kind == "stream_end":
                turn.citations = payload.get("citations") or []
                turn.confidence = payload.get("confidence")
                return turn
            elif kind == "error":
                turn.error = payload
                return turn


def check_turn(turn: Turn, question: dict, bill_id: str | None, min_confidence: float, canonical: bool) -> list[str]:
    problems: list[str] = []
    if turn.error:
        return [f"error frame: {turn.error.get('code')}: {turn.error.get('message')}"]
    if turn.frames[:1] != ["stream_start"] or turn.frames[-1:] != ["stream_end"]:
        problems.append(f"unexpected frame order: {turn.frames[:3]}...{turn.frames[-1:]}")
    if not turn.answer.strip():
        problems.append("empty answer")
    if turn.confidence is None or turn.confidence < min_confidence:
        problems.append(f"confidence {turn.confidence} < {min_confidence}")
    lowered = turn.answer.lower()
    wanted = question.get("expect_any") or []
    if wanted and not any(w.lower() in lowered for w in wanted):
        problems.append(f"answer mentions none of {wanted}")
    for banned in question.get("expect_none") or []:
        if banned.lower() in lowered:
            problems.append(f"answer mentions {banned!r}")
    if len(turn.citations) < question.get("min_citations", 0):
        problems.append(f"{len(turn.citations)} citations, expected at least {question['min_citations']}")
    if canonical and bill_id:
        for citation in turn.citations:
            others = {u for u in UUID_RE.findall(str(citation.get("document_id", "")).lower()) if u != bill_id.lower()}
            if others:
                problems.append(f"citation {citation.get('document_id')} belongs to another bill ({sorted(others)[0]})")
    return problems


def citation_coverage(turn: Turn, bill_id: str | None) -> str:
    """How many citations carried a bill id at all: isolation says nothing about the rest."""
    carrying = sum(1 for c in turn.citations if UUID_RE.search(str(c.get("document_id", "")).lower()))
    return f"{carrying}/{len(turn.citations)} citations carry a bill id (isolation checked on those)"


async def resolve_page_context(http_base: str, ddp_url: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{http_base}/votebot/v1/content/resolve", params={"url": ddp_url})
        response.raise_for_status()
        return response.json()


async def retrieval_checks(case: dict, page_context: dict, expect_types: list[str]) -> tuple[list[str], list[str]]:
    """Chunk-level checks against the index itself (imports votebot lazily: needs its .env)."""
    from votebot.api.schemas.chat import PageContext
    from votebot.config import get_settings
    from votebot.core.retrieval import RetrievalService

    settings = get_settings()
    key = settings.bill_filter_key
    expect_note = f"document types checked: {', '.join(expect_types) or 'none'}"
    wanted = page_context.get(key)
    if not wanted:
        return [f"retrieval check needs page_context[{key!r}] for the {settings.pinecone_index_name} index"], []

    service = RetrievalService(settings)
    question = (case.get("questions") or [DEFAULT_QUESTION])[0]["message"]
    result = await service.retrieve(question, PageContext(**{k: v for k, v in page_context.items() if k in PageContext.model_fields}))
    problems, notes = [], [
        f"this machine's index {settings.pinecone_index_name!r} namespace {settings.pinecone_namespace!r} "
        f"(filter key {key}), {len(result.chunks)} chunks; {expect_note}"
    ]
    if not result.chunks:
        return ["retrieval returned no chunks"], notes

    stray = {c.metadata.get(key) for c in result.chunks if c.metadata.get(key) not in (wanted, None)}
    if stray:
        problems.append(f"chunks of other bills retrieved: {sorted(map(str, stray))}")
    types = {c.metadata.get("document_type") for c in result.chunks}
    for missing in sorted(set(expect_types) - types):
        problems.append(f"no {missing} chunk retrieved (got {sorted(map(str, types))})")

    if result.current_document_id:  # canonical index with a known current version
        text_ids = {str(c.metadata.get("document_id")) for c in result.chunks if c.metadata.get("document_type") == "bill-text"}
        if text_ids and text_ids != {result.current_document_id}:
            problems.append(f"bill-text document_id {sorted(text_ids)} != current version {result.current_document_id}")
        else:
            notes.append(f"current version {result.current_document_id} matches the vectors' document_id")
    return problems, notes


async def run_case(case: dict, args: argparse.Namespace) -> Result:
    result = Result(case.get("name") or "unnamed case")
    page_context = case.get("page_context")
    try:
        if not page_context and case.get("ddp_url"):
            if not args.resolve_base:
                return Result(result.name, ["case has ddp_url only; pass --resolve-base to resolve it"])
            page_context = await resolve_page_context(args.resolve_base, case["ddp_url"])
        page_context = page_context or {"type": "general"}
        bill_id = page_context.get("ocd_bill_id")
        if bill_id == PLACEHOLDER_BILL:
            return Result(result.name, ["case still has the placeholder ocd_bill_id; use a bill that is embedded"])
        session_id = uuid.uuid4().hex[:12]
        for question in case.get("questions") or [DEFAULT_QUESTION]:
            turn = await asyncio.wait_for(
                ask(args.url, session_id, question["message"], page_context, args.timeout), args.turn_timeout
            )
            for problem in check_turn(turn, question, bill_id, args.min_confidence, args.index == "canonical"):
                result.problems.append(f"{question['message']!r}: {problem}")
            result.notes.append(
                f"{question['message']!r}: {len(turn.answer)} chars, confidence {turn.confidence}, "
                + citation_coverage(turn, bill_id)
            )
        if args.retrieval:
            problems, notes = await asyncio.wait_for(
                retrieval_checks(case, page_context, case.get("expect_types") or args.expect_types), args.timeout
            )
            result.problems += problems
            result.notes += notes
    except Exception as e:  # noqa: BLE001 -- a connection or timeout problem is a failed case, not a crash
        result.problems.append(f"{type(e).__name__}: {e}")
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="ws://localhost:8000/ws/chat", help="the instance's /ws/chat URL")
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("smoke_cases.json"))
    parser.add_argument("--index", choices=["canonical", "legacy"], default="canonical",
                        help="which index the instance reads; canonical enables the citation isolation check")
    parser.add_argument("--resolve-base", help="HTTP base of the instance, to resolve cases that give only a ddp_url")
    parser.add_argument("--min-confidence", type=float, default=0.5)
    parser.add_argument("--timeout", type=float, default=90.0, help="seconds to wait for each frame, and for the retrieval check")
    parser.add_argument("--turn-timeout", type=float, default=240.0, help="seconds allowed for a whole question")
    parser.add_argument("--retrieval", action="store_true", help="also check the retrieved chunks (needs the .env keys)")
    parser.add_argument("--expect-types", type=lambda s: [t for t in s.split(",") if t], default=["bill-text"],
                        help="comma-separated document types retrieval must return (cases may override)")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cases = json.loads(args.cases.read_text())
    print(f"target {args.url} ({args.index} index expected), cases {args.cases}, {len(cases)} case(s)")
    if args.retrieval:
        print("--retrieval reads the index from THIS machine's .env: it proves the target only if its "
              "PINECONE_INDEX_NAME and PINECONE_NAMESPACE are the same")
    print()
    results = [await run_case(case, args) for case in cases]
    for r in results:
        print(f"{'PASS' if r.ok else 'FAIL'}  {r.name}")
        for line in r.notes:
            print(f"        {line}")
        for line in r.problems:
            print(f"      ! {line}")
    failed = [r for r in results if not r.ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
