"""VOTEBOT-8: retrieval filters bills by `ocd_bill_id` on the canonical-id index.

PLAN-enterprise-search.md 5.6: one setting (the index name) decides both which index is read
and which metadata key pins a bill, so rolling back to `votebot-large` is a single change.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from votebot.api.schemas.chat import PageContext
from votebot.config import LEGACY_PINECONE_INDEX_NAME, Settings
from votebot.core.retrieval import RetrievalConfig, RetrievalService
from votebot.services.vector_store import SearchResult

NEW_INDEX = "ddp-knowledge-base"
BILL = "a3f7c0d1-1111-4222-8333-444455556666"


def _settings(index: str) -> Settings:
    return Settings(pinecone_index_name=index, _env_file=None)


class _FakeVersions:
    """Stands in for BillVersionService: returns the given versions (None = api-v3 cannot say)."""

    def __init__(self, versions=None):
        self.versions = versions
        self.asked: list[str] = []

    async def get_versions(self, ocd_bill_id):
        self.asked.append(ocd_bill_id)
        return self.versions


def _service(
    index: str = NEW_INDEX, queries: list | None = None, respond=None, versions=None
) -> RetrievalService:
    """A RetrievalService with a recording fake vector store (no network)."""
    svc = RetrievalService.__new__(RetrievalService)
    svc.settings = _settings(index)
    svc.bill_versions = _FakeVersions(versions)
    svc.config = RetrievalConfig(max_chunks=10, similarity_threshold=0.1, deduplicate=False)
    calls = queries if queries is not None else []

    async def query(query, top_k=10, filter=None, include_metadata=True):
        calls.append(filter)
        return respond(filter) if respond else []

    svc.vector_store = AsyncMock()
    svc.vector_store.query = query
    return svc


def _hit(doc_type: str, **metadata) -> SearchResult:
    return SearchResult(
        id=f"{doc_type}-{metadata.get('ocd_bill_id', 'x')}-chunk-0",
        content=f"{doc_type} content",
        score=0.9,
        metadata={"document_type": doc_type, **metadata},
    )


def _matches(metadata: dict, flt: dict) -> bool:
    """Pinecone's metadata filter semantics for the operators this code uses: equality, $in, $nin."""
    for key, want in (flt or {}).items():
        have = metadata.get(key)
        if isinstance(want, dict):
            if "$in" in want and have not in want["$in"]:
                return False
            if "$nin" in want and have in want["$nin"]:
                return False
        elif have != want:
            return False
    return True


class TestBillFilterKey:
    def test_legacy_index_filters_by_webflow_id(self):
        assert _settings(LEGACY_PINECONE_INDEX_NAME).bill_filter_key == "webflow_id"

    def test_default_index_is_the_legacy_one(self):
        # The flag-off path must be unchanged: nothing switches until the index setting does.
        assert Settings(_env_file=None).bill_filter_key == "webflow_id"

    def test_any_other_index_filters_by_ocd_bill_id(self):
        assert _settings(NEW_INDEX).bill_filter_key == "ocd_bill_id"

    def test_rollback_is_one_setting(self):
        new = _settings(NEW_INDEX)
        rolled_back = new.model_copy(update={"pinecone_index_name": LEGACY_PINECONE_INDEX_NAME})
        assert (new.bill_filter_key, rolled_back.bill_filter_key) == ("ocd_bill_id", "webflow_id")


class TestPageContextField:
    def test_page_context_carries_ocd_bill_id(self):
        ctx = PageContext(type="bill", ocd_bill_id=BILL)
        assert ctx.ocd_bill_id == BILL
        assert PageContext(type="bill").ocd_bill_id is None


class TestBuildFilters:
    def test_ocd_mode_bill_uses_ocd_bill_id_only(self):
        ctx = PageContext(type="bill", ocd_bill_id=BILL, webflow_id="wf1", slug="a-slug")
        assert _service()._build_filters(ctx) == {"ocd_bill_id": BILL}

    def test_ocd_mode_bill_without_id_has_no_identity_filter(self):
        ctx = PageContext(type="bill", webflow_id="wf1", slug="a-slug")
        assert _service()._build_filters(ctx) == {}

    def test_legacy_mode_bill_is_unchanged(self):
        svc = _service(LEGACY_PINECONE_INDEX_NAME)
        assert svc._build_filters(PageContext(type="bill", webflow_id="wf1", ocd_bill_id=BILL)) == {
            "webflow_id": "wf1"
        }
        assert svc._build_filters(PageContext(type="bill", slug="a-slug")) == {"slug": "a-slug"}

    def test_legislator_filters_are_unchanged_in_both_modes(self):
        ctx = PageContext(type="legislator", id="ocd-person/xyz")
        assert _service()._build_filters(ctx) == {"legislator_id": "ocd-person/xyz"}
        assert _service(LEGACY_PINECONE_INDEX_NAME)._build_filters(ctx) == {"legislator_id": "ocd-person/xyz"}


class TestLegislativeScope:
    def test_general_page_with_jurisdiction_and_session(self):
        ctx = PageContext(type="general", jurisdiction="fl", session="2026")
        assert _service()._legislative_scope(ctx) == {"jurisdiction": "FL", "session_code": "2026"}

    def test_jurisdiction_alone_is_enough(self):
        assert _service()._legislative_scope(PageContext(type="general", jurisdiction="US")) == {
            "jurisdiction": "US"
        }

    def test_no_scope_without_a_jurisdiction(self):
        assert _service()._legislative_scope(PageContext(type="general", session="2026")) == {}

    def test_legacy_index_has_no_scope(self):
        # On the legacy index `jurisdiction` is a Webflow id, so a code filter would match nothing.
        svc = _service(LEGACY_PINECONE_INDEX_NAME)
        assert svc._legislative_scope(PageContext(type="general", jurisdiction="FL")) == {}

    def test_bill_pages_are_not_scoped_this_way(self):
        assert _service()._legislative_scope(PageContext(type="bill", jurisdiction="FL", ocd_bill_id=BILL)) == {}


class TestIdentityFilter:
    def test_ocd_mode_uses_ocd_bill_id_and_never_a_slug(self):
        assert _service()._identity_filter({"ocd_bill_id": BILL, "slug": "s"}) == {"ocd_bill_id": BILL}
        # Vectors on the canonical-id index carry no slug, so a slug filter would match nothing.
        assert _service()._identity_filter({"slug": "s"}) == {}

    def test_legacy_mode_prefers_webflow_id_then_slug(self):
        svc = _service(LEGACY_PINECONE_INDEX_NAME)
        assert svc._identity_filter({"webflow_id": "wf1", "slug": "s"}) == {"webflow_id": "wf1"}
        assert svc._identity_filter({"slug": "s"}) == {"slug": "s"}
        assert svc._identity_filter({}) == {}


class TestRetrieveOnCanonicalIndex:
    async def test_bill_page_without_ocd_bill_id_returns_nothing_and_queries_nothing(self):
        calls: list = []
        svc = _service(queries=calls)
        result = await svc.retrieve("what does this bill do?", PageContext(type="bill", webflow_id="wf1", slug="s"))
        assert result.chunks == [] and result.total_retrieved == 0
        assert calls == []  # never searched without the filter that isolates the bill

    async def test_every_bill_scoped_query_carries_the_ocd_bill_id(self):
        calls: list = []
        svc = _service(queries=calls, respond=lambda f: [_hit(f["document_type"], ocd_bill_id=BILL)])
        ctx = PageContext(type="bill", ocd_bill_id=BILL)

        result = await svc.retrieve("how did senators vote on this bill?", ctx)

        scoped = [f for f in calls if f and f.get("document_type") in {"bill-text", "bill", "bill-votes"}]
        assert {f["document_type"] for f in scoped} >= {"bill-text", "bill-votes"}
        assert all(f.get("ocd_bill_id") == BILL and "webflow_id" not in f for f in scoped)
        assert result.filters_applied == {"ocd_bill_id": BILL}

    async def test_a_query_about_one_bill_returns_only_that_bill(self):
        other = "99999999-0000-4000-8000-000000000000"

        def respond(f):
            # A faithful stand-in for Pinecone: only return chunks matching the metadata filter.
            pool = [_hit("bill-text", ocd_bill_id=BILL), _hit("bill-text", ocd_bill_id=other)]
            return [c for c in pool if all(c.metadata.get(k) == v for k, v in f.items() if k == "ocd_bill_id")]

        svc = _service(respond=respond)
        result = await svc.retrieve("summarize", PageContext(type="bill", ocd_bill_id=BILL))
        assert result.chunks and {c.metadata["ocd_bill_id"] for c in result.chunks} == {BILL}

    async def test_named_bill_on_a_general_page_is_looked_up_by_gov_id_and_jurisdiction(self):
        calls: list = []

        def respond(f):
            if "gov_id" in f:
                return [_hit("bill-text", ocd_bill_id=BILL, session_code="2025", gov_id="HB 363")]
            return [_hit(f["document_type"], ocd_bill_id=f["ocd_bill_id"])] if f.get("ocd_bill_id") else []

        svc = _service(queries=calls, respond=respond)
        result = await svc.retrieve("What does Florida HB 363 do?", PageContext(type="general"))

        lookup = next(f for f in calls if f and "gov_id" in f)
        assert lookup == {"document_type": "bill-text", "gov_id": "HB 363", "jurisdiction": "FL"}
        assert result.filters_applied == {"ocd_bill_id": BILL}
        assert result.chunks

    async def test_a_bill_matching_several_sessions_is_not_guessed(self):
        # Every session has its own "HB 363", and "2026D" sorts after "2026" as text, so picking
        # one would risk answering about the wrong bill.
        calls: list = []

        def respond(f):
            if f and "gov_id" in f:
                return [
                    _hit("bill-text", ocd_bill_id=BILL, session_code="2026", gov_id="HB 363"),
                    _hit("bill-text", ocd_bill_id="special", session_code="2026D", gov_id="HB 363"),
                ]
            return []

        svc = _service(queries=calls, respond=respond)
        result = await svc.retrieve("What does Florida HB 363 do?", PageContext(type="general"))
        assert result.filters_applied == {}  # stayed a general query, no bill chosen
        assert not any(f and f.get("ocd_bill_id") for f in calls)

    async def test_a_page_that_names_its_session_disambiguates_the_bill(self):
        calls: list = []

        def respond(f):
            if "gov_id" in f:
                assert f["session_code"] == "2026"  # the lookup is narrowed to the page's session
                return [_hit("bill-text", ocd_bill_id=BILL, session_code="2026", gov_id="HB 363")]
            return [_hit(f["document_type"], ocd_bill_id=f["ocd_bill_id"])] if f.get("ocd_bill_id") else []

        svc = _service(queries=calls, respond=respond)
        result = await svc.retrieve(
            "What does Florida HB 363 do?", PageContext(type="general", session="2026")
        )
        assert result.filters_applied == {"ocd_bill_id": BILL}

    async def test_named_bill_without_any_jurisdiction_is_not_guessed(self):
        calls: list = []
        svc = _service(queries=calls)
        await svc.retrieve("What does HB 363 do?", PageContext(type="general"))
        assert not any(f and "gov_id" in f for f in calls)

    async def test_general_page_scope_adds_a_bill_text_and_votes_query_first(self):
        calls: list = []

        def respond(f):
            if f and "session_code" in f:
                return [_hit("bill-text", ocd_bill_id=BILL)]
            return [_hit("legislator", legislator_id="p1")]

        svc = _service(queries=calls, respond=respond)
        result = await svc.retrieve("housing bills", PageContext(type="general", jurisdiction="fl", session="2026"))

        scoped = next(f for f in calls if f and "session_code" in f)
        assert scoped == {
            "jurisdiction": "FL",
            "session_code": "2026",
            "document_type": {"$in": ["bill-text", "bill-votes"]},
        }
        # The scoped bill text leads, and the unscoped query still runs so legislators survive.
        assert [c.metadata["document_type"] for c in result.chunks] == ["bill-text", "legislator"]

    async def test_other_jurisdictions_bill_text_is_kept_out_of_a_scoped_general_page(self):
        pool = [
            _hit("bill-text", ocd_bill_id=BILL, jurisdiction="FL", session_code="2026"),
            _hit("bill-text", ocd_bill_id="tx-bill", jurisdiction="TX", session_code="2026"),
            _hit("bill-votes", ocd_bill_id="tx-votes", jurisdiction="TX", session_code="2026"),
            _hit("legislator", legislator_id="p1", jurisdiction="FL"),
            _hit("organization", organization_id="o1"),
        ]
        # A store that honours the filter exactly as Pinecone does.
        svc = _service(respond=lambda f: [c for c in pool if _matches(c.metadata, f)])

        result = await svc.retrieve("housing", PageContext(type="general", jurisdiction="FL", session="2026"))

        ids = [c.metadata.get("ocd_bill_id") or c.metadata.get("legislator_id") or c.metadata.get("organization_id")
               for c in result.chunks]
        assert ids == [BILL, "p1", "o1"]  # FL bill text first; legislators and orgs survive; TX bills are out

    async def test_general_page_without_scope_issues_one_query(self):
        calls: list = []
        svc = _service(queries=calls)
        await svc.retrieve("how do I register to vote", PageContext(type="general"))
        assert len(calls) == 1


class TestLegacyIndexIsUntouched:
    async def test_general_page_still_uses_the_slug_lookup(self):
        svc = _service(LEGACY_PINECONE_INDEX_NAME)
        with patch.object(svc, "_lookup_bill_slug", AsyncMock(return_value=None)) as lookup:
            await svc.retrieve("What does Florida HB 363 do?", PageContext(type="general"))
        lookup.assert_awaited_once()

    async def test_bill_page_filters_by_webflow_id(self):
        calls: list = []
        svc = _service(LEGACY_PINECONE_INDEX_NAME, queries=calls)
        await svc.retrieve("summarize", PageContext(type="bill", webflow_id="wf1", ocd_bill_id=BILL))
        typed = [f for f in calls if f and f.get("document_type") == "bill-text"]
        assert typed and all(f.get("webflow_id") == "wf1" and "ocd_bill_id" not in f for f in typed)
