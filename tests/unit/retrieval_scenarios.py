"""A deterministic stand-in for Pinecone and the retrieval scenarios the concurrency tests replay (VOTEBOT-45).

`FakeStore.query` answers like Pinecone for the filters the retrieval code uses: it applies the metadata filter,
sorts by score (best first, id as the tie-break) and cuts to `top_k`. The scenarios run the REAL RetrievalService
phases over it, so the order and the cut of every phase's results matter to the final chunks.
"""

from __future__ import annotations

import asyncio

from tests.unit.test_ocd_bill_id_retrieval import BILL, NEW_INDEX, _matches
from votebot.api.schemas.chat import PageContext
from votebot.config import LEGACY_PINECONE_INDEX_NAME, Settings
from votebot.core.retrieval import RetrievalConfig, RetrievalService
from votebot.services.vector_store import SearchResult

OTHER_BILL = "b4a8d1e2-2222-4333-8444-555566667777"
TITLE = "Early Voting Tabulation"


def chunk(doc_type: str, n: int, score: float, **meta) -> SearchResult:
    content = meta.pop("content", f"{doc_type} {n}")
    return SearchResult(id=f"{doc_type}-{n}", content=content, score=score, metadata={"document_type": doc_type, **meta})


def corpus(n_text: int, n_summary: int, n_org: int = 0, mention: bool = False) -> list[SearchResult]:
    items = [chunk("bill-text", i, 0.95 - i * 0.03, ocd_bill_id=BILL, document_id="doc-1") for i in range(n_text)]
    items += [chunk("bill", i, 0.80 - i * 0.04, ocd_bill_id=BILL) for i in range(n_summary)]
    for i in range(n_org):
        text = f"organization {i}" + (f" supports {TITLE.lower()}" if mention and i % 2 else "")
        items.append(chunk("organization", i, 0.70 - i * 0.02, document_id=f"org-{i}", broker_org_id=str(i), content=text))
    items += [chunk("bill-text", 100 + i, 0.99, ocd_bill_id=OTHER_BILL) for i in range(3)]  # another bill: never returned
    return items


class FakeStore:
    def __init__(self, items: list[SearchResult], delay: float = 0.0):
        self.items = items
        self.delay = delay
        self.calls: list[dict] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.tasks: list[asyncio.Task] = []

    async def query(self, query, top_k=10, filter=None, include_metadata=True):
        self.calls.append({"query": query, "top_k": top_k, "filter": filter})
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        hits = [r for r in self.items if _matches(r.metadata, filter or {})]
        hits.sort(key=lambda r: (-r.score, r.id))
        return hits[:top_k]


class FakeVersions:
    async def get_versions(self, ocd_bill_id):
        return None  # no version filter: every version's chunks come back


def make_service(items, index=NEW_INDEX, delay=0.0, diffs=None) -> tuple[RetrievalService, FakeStore]:
    svc = RetrievalService.__new__(RetrievalService)
    svc.settings = Settings(pinecone_index_name=index, _env_file=None)
    svc.bill_versions = FakeVersions()
    svc.config = RetrievalConfig(max_chunks=10, similarity_threshold=0.1, deduplicate=True)
    store = FakeStore(items, delay)
    svc.vector_store = store

    async def live_diffs(ocd_bill_id, query, notes=None):
        await asyncio.sleep(delay)
        return list(diffs or [])

    svc._live_version_diffs = live_diffs
    return svc, store


BILL_PAGE = PageContext(type="bill", id="HB 1", title=TITLE, jurisdiction="FL", session="2026", ocd_bill_id=BILL)
ORG_PAGE = PageContext(type="organization", id="3", title="Some Org")
GENERAL_FL = PageContext(type="general", title="Bills list", jurisdiction="FL", session="2026")

# name -> (corpus args, index, page, question)
SCENARIOS = {
    "text_fills_every_slot": (dict(n_text=12, n_summary=5), NEW_INDEX, BILL_PAGE, "What does this bill do?"),
    "some_text_summary_fills_the_rest": (dict(n_text=3, n_summary=8), NEW_INDEX, BILL_PAGE, "What does this bill do?"),
    "no_text_only_summary": (dict(n_text=0, n_summary=6), NEW_INDEX, BILL_PAGE, "What does this bill do?"),
    "nothing_for_the_bill_falls_back": (dict(n_text=0, n_summary=0), NEW_INDEX, BILL_PAGE, "What does this bill do?"),
    "org_question_prefers_orgs_that_name_the_bill": (dict(n_text=4, n_summary=3, n_org=9, mention=True), NEW_INDEX, BILL_PAGE, "Which organizations support this bill?"),
    "org_question_with_text_filling_the_slots": (dict(n_text=12, n_summary=3, n_org=6, mention=True), NEW_INDEX, BILL_PAGE, "Who supports this?"),
    "org_question_without_any_org_documents": (dict(n_text=5, n_summary=4, n_org=0), NEW_INDEX, BILL_PAGE, "Who opposes it?"),
    "legacy_index_vote_question": (dict(n_text=2, n_summary=3), LEGACY_PINECONE_INDEX_NAME, PageContext(type="bill", id="HB 1", title=TITLE, webflow_id="wf-1"), "How did the vote go?"),
    "organization_page": (dict(n_text=0, n_summary=0, n_org=8), NEW_INDEX, ORG_PAGE, "What does this organization stand for?"),
    "general_page_scoped_to_a_jurisdiction": (dict(n_text=6, n_summary=2), NEW_INDEX, GENERAL_FL, "what bills are on this page?"),
}


def prepare(name: str, **kw) -> tuple[RetrievalService, FakeStore, PageContext, str]:
    args, index, page, question = SCENARIOS[name]
    items = corpus(**args)
    if index == LEGACY_PINECONE_INDEX_NAME:  # the legacy index keys a bill by webflow_id
        for r in items:
            if r.metadata.get("ocd_bill_id") == BILL:
                r.metadata["webflow_id"] = "wf-1"
        items += [chunk("bill-votes", i, 0.9 - i * 0.05, webflow_id="wf-1") for i in range(4)]
    if name == "general_page_scoped_to_a_jurisdiction":
        for r in items:
            r.metadata.update({"jurisdiction": "FL", "session_code": "2026"} if r.metadata["document_type"] == "bill-text" else {})
    svc, store = make_service(items, index, **kw)
    return svc, store, page, question


async def run(name: str, **kw) -> tuple[list[str], FakeStore]:
    svc, store, page, question = prepare(name, **kw)
    result = await svc.retrieve(query=question, page_context=page)
    return [c.id for c in result.chunks], store
