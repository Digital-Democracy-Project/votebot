"""VOTEBOT-45: the independent retrieval queries run concurrently, and the answer does not change.

GOLDEN holds the chunk ids the SEQUENTIAL code returned for each scenario in retrieval_scenarios.py (captured
before the change, over the same deterministic stand-in for Pinecone). The concurrent code must return the same
chunks in the same order: _deduplicate is order-sensitive and the bill-text-first priority is the point of the phases.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.unit.retrieval_scenarios import (
    SCENARIOS,
    prepare,
    run,
)

GOLDEN = {
    'text_fills_every_slot': ['bill-text-0', 'bill-text-1', 'bill-text-2', 'bill-text-3', 'bill-text-4', 'bill-text-5', 'bill-text-6', 'bill-text-7', 'bill-text-8', 'bill-text-9'],
    'some_text_summary_fills_the_rest': ['bill-text-0', 'bill-text-1', 'bill-text-2', 'bill-0', 'bill-1', 'bill-2', 'bill-3', 'bill-4', 'bill-5', 'bill-6'],
    'no_text_only_summary': ['bill-0', 'bill-1', 'bill-2', 'bill-3', 'bill-4', 'bill-5'],
    'nothing_for_the_bill_falls_back': [],
    'org_question_prefers_orgs_that_name_the_bill': ['bill-0', 'bill-1', 'bill-2', 'organization-1', 'organization-3', 'organization-0', 'organization-2', 'bill-text-0', 'bill-text-1', 'bill-text-2'],
    'org_question_with_text_filling_the_slots': ['bill-0', 'bill-1', 'bill-2', 'organization-1', 'organization-3', 'organization-0', 'organization-2', 'bill-text-0', 'bill-text-1', 'bill-text-2'],
    'org_question_without_any_org_documents': ['bill-0', 'bill-1', 'bill-2', 'bill-3', 'bill-text-0', 'bill-text-1', 'bill-text-2', 'bill-text-3', 'bill-text-4'],
    'legacy_index_vote_question': ['bill-votes-0', 'bill-votes-1', 'bill-votes-2', 'bill-votes-3', 'bill-text-0', 'bill-text-1', 'bill-0', 'bill-1', 'bill-2'],
    'organization_page': ['organization-3'],
    'general_page_scoped_to_a_jurisdiction': ['bill-text-100', 'bill-text-101', 'bill-text-102', 'bill-text-0', 'bill-text-1', 'bill-text-2', 'bill-text-3', 'bill-text-4', 'bill-text-5', 'bill-0'],
}


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_the_same_chunks_in_the_same_order_as_the_sequential_code(name):
    ids, _store = await run(name)
    assert ids == GOLDEN[name]


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_the_same_answer_when_every_query_takes_time(name):
    ids, _store = await run(name, delay=0.01)
    assert ids == GOLDEN[name]


class TestTheQueriesOverlap:
    async def test_an_organization_question_runs_its_four_queries_together(self):
        ids, store = await run("org_question_prefers_orgs_that_name_the_bill", delay=0.03)
        assert ids == GOLDEN["org_question_prefers_orgs_that_name_the_bill"]
        assert len(store.calls) == 4 and store.max_in_flight == 4  # text, summary, the bill's org chunks, organizations

    async def test_a_plain_question_overlaps_the_text_and_summary_queries(self):
        _ids, store = await run("some_text_summary_fills_the_rest", delay=0.03)
        assert store.max_in_flight == 2

    async def test_the_scoped_general_page_runs_both_of_its_queries_together(self):
        _ids, store = await run("general_page_scoped_to_a_jurisdiction", delay=0.03)
        assert store.max_in_flight == 2

    async def test_an_organization_page_asks_for_its_general_results_while_the_first_search_runs(self):
        _ids, store = await run("organization_page", delay=0.03)
        assert store.max_in_flight == 2

    async def test_the_changelog_diffs_are_read_while_the_text_is_searched(self):
        svc, store, page, _q = prepare("some_text_summary_fills_the_rest", delay=0.03)
        started = []
        real = svc._live_version_diffs

        async def watched(*a, **k):
            started.append(store.in_flight)  # queries already running when the diffs are asked for
            return await real(*a, **k)

        svc._live_version_diffs = watched
        await svc.retrieve(query="What changed in this bill?", page_context=page)
        assert started and started[0] >= 1

    async def test_total_time_is_near_the_slowest_query_not_the_sum(self):
        svc, store, page, _q = prepare("org_question_prefers_orgs_that_name_the_bill", delay=0.1)
        loop = asyncio.get_running_loop()
        began = loop.time()
        await svc.retrieve(query="Which organizations support this bill?", page_context=page)
        assert loop.time() - began < 0.25  # four queries of 0.1 s: 0.4 s one after another


class TestEachQueryAsksForAtLeastWhatTheSequentialCodeAsked:
    """A query that now starts before the result it used to depend on asks for the most it could have needed,
    and the phase cuts the answer back to what the sequential code asked for (the result is score-sorted)."""

    async def test_the_summary_query_asks_for_two_chunks_per_slot_a_full_page_could_leave(self):
        _ids, store = await run("some_text_summary_fills_the_rest")
        summary = [c for c in store.calls if c["filter"].get("document_type") == "bill"]
        assert [c["top_k"] for c in summary] == [20]

    async def test_a_summary_query_the_text_made_unnecessary_is_not_used(self):
        ids, _store = await run("text_fills_every_slot")
        assert not any(i.startswith("bill-") and not i.startswith("bill-text") for i in ids)


class TestFailuresAndGivingUp:
    async def test_a_failing_query_fails_the_retrieval_as_it_did_and_leaves_nothing_running(self):
        svc, store, page, _q = prepare("org_question_prefers_orgs_that_name_the_bill", delay=0.02)
        real = store.query

        async def broken(query, top_k=10, filter=None, include_metadata=True):
            if (filter or {}).get("document_type") == "organization":
                raise RuntimeError("pinecone down")
            return await real(query, top_k, filter, include_metadata)

        svc.vector_store.query = broken
        with pytest.raises(RuntimeError, match="pinecone down"):
            await svc.retrieve(query="Which organizations support this bill?", page_context=page)
        await asyncio.sleep(0.05)
        assert store.in_flight == 0  # the other queries finished or were cancelled; none is left behind

    async def test_giving_up_cancels_the_queries_still_running(self):
        svc, store, page, _q = prepare("org_question_prefers_orgs_that_name_the_bill", delay=0.5)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(svc.retrieve(query="Which organizations support this bill?", page_context=page), 0.05)
        await asyncio.sleep(0.05)
        assert store.in_flight == 0

    async def test_the_legacy_vote_phases_still_run_one_after_another(self):
        # the legacy index is frozen (VOTEBOT-17 removes it): its vote queries are unchanged
        _ids, store = await run("legacy_index_vote_question", delay=0.02)
        assert [c["filter"].get("document_type") for c in store.calls if c["filter"].get("document_type") == "bill-votes"] == ["bill-votes"]
