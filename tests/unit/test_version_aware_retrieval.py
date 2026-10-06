"""VOTEBOT-10: retrieval defaults to a bill's CURRENT version and understands version requests.

The fake store applies metadata filters the way Pinecone does ($in / $nin / equality), so these
tests fail if a query is sent without the filter that selects the intended version.
"""

from __future__ import annotations

from tests.unit.test_ocd_bill_id_retrieval import BILL, _matches, _service
from votebot.api.schemas.chat import PageContext
from votebot.services.bill_versions import BillVersion

SAME_PASSAGE = "Section 1. The department shall administer the program."

VERSIONS = [
    BillVersion("101", "Introduced", "2026-01-10", "introduced", 0),
    BillVersion("102", "Engrossed", "2026-03-04", "chamber_passage", 1),
    BillVersion("103", "Enrolled", "2026-04-01", "final_passage", 2),
    BillVersion("999", "Odd note", "2026-05-01", "unknown", None),
]


def _meta(doc_id, doc_type="bill-text", **extra):
    version = next(v for v in VERSIONS if v.document_id == doc_id)
    return {
        "document_type": doc_type,
        "ocd_bill_id": BILL,
        "gov_id": "HB 1",
        "document_id": doc_id,
        "version_note": version.note,
        "version_date": version.date,
        "version_stage": version.stage,
        **extra,
    }


def _chunk(doc_id, doc_type="bill-text", content=SAME_PASSAGE, **extra):
    from votebot.services.vector_store import SearchResult

    return SearchResult(
        id=f"{doc_type}:{BILL}:{doc_id}-chunk-0", content=content, score=0.9, metadata=_meta(doc_id, doc_type, **extra)
    )


# Three versions that share the same passage verbatim. (Diffs are not embedded: they are read live.)
POOL = [_chunk("101"), _chunk("102"), _chunk("103")]


def _store(calls: list):
    def respond(f):
        calls.append(f)
        return [c for c in POOL if _matches(c.metadata, f)]

    return respond


def _svc(calls, versions=VERSIONS):
    return _service(queries=[], respond=_store(calls), versions=versions)


def _bill_context():
    return PageContext(type="bill", ocd_bill_id=BILL)


def _text_filter(calls):
    return next(f for f in calls if f and f.get("document_type") == "bill-text")


class TestVersionScope:
    async def test_defaults_to_the_current_version(self):
        scope = await _svc([])._version_scope(BILL, "what does this bill do?")
        assert scope == ("103", {"document_id": "103"})  # the unknown-stage "999" is never current

    async def test_a_named_stage_replaces_the_current_version_filter(self):
        current, flt = await _svc([])._version_scope(BILL, "show me the bill as introduced")
        assert current == "103"  # still reported, so the prompt can say which one is current
        assert flt == {"version_stage": {"$in": ["introduced"]}}

    async def test_a_named_date(self):
        _, flt = await _svc([])._version_scope(BILL, "the version from March 4, 2026")
        assert flt == {"version_date": {"$in": ["2026-03-04"]}}

    async def test_unknown_current_version_means_no_filter(self):
        assert await _svc([], versions=None)._version_scope(BILL, "what does this bill do?") == (None, {})
        assert await _svc([], versions=[VERSIONS[3]])._version_scope(BILL, "what does this bill do?") == (None, {})

    async def test_an_unarchived_latest_version_does_not_promote_an_older_one(self):
        versions = [VERSIONS[0], VERSIONS[1], BillVersion(None, "Newest", "2026-06-01", "enacted", 2)]
        assert await _svc([], versions=versions)._version_scope(BILL, "what does this bill do?") == (None, {})

    async def test_a_date_in_an_ordinary_question_is_not_a_version_request(self):
        current, flt = await _svc([])._version_scope(BILL, "does this take effect March 4, 2026?")
        assert (current, flt) == ("103", {"document_id": "103"})


class TestRetrieveDefaultsToTheCurrentVersion:
    async def test_a_question_about_a_multi_version_bill_is_answered_from_the_current_version(self):
        calls: list = []
        result = await _svc(calls).retrieve("what does this bill do?", _bill_context())

        assert _text_filter(calls)["document_id"] == "103"
        assert {c.metadata["document_id"] for c in result.chunks} == {"103"}
        assert result.current_document_id == "103"

    async def test_a_named_stage_retrieves_that_version_instead(self):
        calls: list = []
        result = await _svc(calls).retrieve("what does the engrossed version say?", _bill_context())

        assert "document_id" not in _text_filter(calls)
        assert _text_filter(calls)["version_stage"] == {"$in": ["chamber_passage"]}
        assert {c.metadata["document_id"] for c in result.chunks} == {"102"}
        assert result.current_document_id == "103"

    async def test_when_api_v3_cannot_say_every_version_comes_back_labelled(self):
        calls: list = []
        result = await _svc(calls, versions=None).retrieve("what does this bill do?", _bill_context())

        assert "document_id" not in _text_filter(calls) and "version_stage" not in _text_filter(calls)
        assert {c.metadata["document_id"] for c in result.chunks} == {"101", "102", "103"}
        assert result.current_document_id is None

    async def test_the_legacy_index_never_asks_for_versions(self):
        from votebot.config import LEGACY_PINECONE_INDEX_NAME

        svc = _service(LEGACY_PINECONE_INDEX_NAME, queries=[], versions=VERSIONS)
        result = await svc.retrieve("what does this bill do?", PageContext(type="bill", webflow_id="wf1"))
        assert svc.bill_versions.asked == [] and result.current_document_id is None


class TestFallbackKeepsTheVersionScope:
    """The 'no typed results' fallback must not widen a version search to every version."""

    async def test_a_current_version_with_no_text_yet_returns_nothing_not_older_versions(self):
        pool_without_current = [c for c in POOL if c.metadata["document_id"] != "103"]
        calls: list = []

        def respond(f):
            calls.append(f)
            return [c for c in pool_without_current if _matches(c.metadata, f)]

        svc = _service(queries=[], respond=respond, versions=VERSIONS)
        result = await svc.retrieve("what does this bill do?", _bill_context())

        assert result.chunks == []  # the older versions exist in the index, and must not be served as current
        assert calls[-1] == {"ocd_bill_id": BILL, "document_id": "103"}  # the fallback kept the scope

    async def test_a_named_version_that_does_not_exist_returns_nothing_not_other_versions(self):
        calls: list = []
        result = await _svc(calls).retrieve("show me the bill as enacted", _bill_context())
        assert result.chunks == []
        assert calls[-1] == {"ocd_bill_id": BILL, "version_stage": {"$in": ["enacted"]}}

    async def test_with_no_known_current_version_the_fallback_still_stays_on_this_bill(self):
        calls: list = []
        await _svc(calls, versions=None).retrieve("what does this bill do?", _bill_context())
        assert all(f and f.get("ocd_bill_id") == BILL for f in calls if f and f.get("document_type") != "organization")


class TestWhatChangedIsNotSearchedInTheIndex:
    """Diffs are read live from api-v3 (tests/unit/test_live_version_diffs.py); nothing is searched."""

    async def test_a_changelog_question_sends_no_diff_or_changelog_query_to_the_index(self):
        calls: list = []
        await _svc(calls).retrieve("what changed in this bill?", _bill_context())
        assert not any(f and f.get("document_type") in ("bill-version-diff", "bill-changelog") for f in calls)

    async def test_a_normal_question_does_not_pull_in_diffs(self):
        calls: list = []
        await _svc(calls).retrieve("what does this bill do?", _bill_context())
        assert not any(f and f.get("document_type") == "bill-version-diff" for f in calls)

    async def test_the_legacy_index_still_uses_bill_changelog(self):
        from votebot.config import LEGACY_PINECONE_INDEX_NAME

        calls: list = []
        svc = _service(LEGACY_PINECONE_INDEX_NAME, queries=calls, versions=VERSIONS)
        await svc.retrieve("what changed in this bill?", PageContext(type="bill", webflow_id="wf1"))
        assert any(f and f.get("document_type") == "bill-changelog" for f in calls)
        assert not any(f and f.get("document_type") == "bill-version-diff" for f in calls)


class TestDeduplicateKeepsTheVersion:
    def test_identical_passages_from_two_versions_are_both_kept(self):
        svc = _svc([])
        kept = svc._deduplicate([_chunk("101"), _chunk("102"), _chunk("103")])
        assert [c.metadata["document_id"] for c in kept] == ["101", "102", "103"]

    def test_a_repeated_passage_within_one_version_still_collapses(self):
        svc = _svc([])
        assert len(svc._deduplicate([_chunk("103"), _chunk("103")])) == 1

    def test_the_legacy_index_keeps_its_content_only_key(self):
        from votebot.config import LEGACY_PINECONE_INDEX_NAME

        svc = _service(LEGACY_PINECONE_INDEX_NAME, queries=[], versions=VERSIONS)
        assert len(svc._deduplicate([_chunk("101"), _chunk("102")])) == 1

    def test_chunks_without_a_document_id_still_dedupe_as_before(self):
        from votebot.services.vector_store import SearchResult

        a = SearchResult(id="a", content="same", score=0.9, metadata={})
        b = SearchResult(id="b", content="same", score=0.8, metadata={})
        assert len(_svc([])._deduplicate([a, b])) == 1
