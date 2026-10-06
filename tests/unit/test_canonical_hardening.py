"""VOTEBOT-11: hardening of the canonical-index retrieval added in VOTEBOT-8."""

from __future__ import annotations

import pytest

from tests.unit.test_ocd_bill_id_retrieval import BILL, NEW_INDEX, _hit, _service, _settings
from votebot.api.routes.websocket import _page_context_from_payload
from votebot.api.schemas.chat import PageContext
from votebot.config import LEGACY_PINECONE_INDEX_NAME, Settings
from votebot.core.retrieval import BILL_LOOKUP_TOP_K


class TestIndexNameSwitch:
    @pytest.mark.parametrize(
        "name", ["votebot-dev", "votebot", "votebot-large2", "ddp-knowledgebase", "ddp-knowledge-base-x"]
    )
    def test_unrecognized_names_stay_in_legacy_mode(self, name):
        s = _settings(name)
        assert s.bill_filter_key == "webflow_id"
        assert not s.index_is_recognized

    @pytest.mark.parametrize("name", ["Votebot-Large", "votebot-large ", "  VOTEBOT-LARGE\n", "", "   "])
    def test_legacy_name_is_normalized_and_empty_means_default(self, name):
        s = _settings(name)
        assert s.pinecone_index_name == LEGACY_PINECONE_INDEX_NAME
        assert s.bill_filter_key == "webflow_id"
        assert s.index_is_recognized

    @pytest.mark.parametrize("name", [NEW_INDEX, "DDP-Knowledge-Base", " ddp-knowledge-base "])
    def test_canonical_name_selects_ocd_mode_after_normalizing(self, name):
        s = _settings(name)
        assert s.pinecone_index_name == NEW_INDEX  # the normalized name is also what Pinecone gets
        assert s.bill_filter_key == "ocd_bill_id"
        assert s.index_is_recognized

    def test_canonical_name_is_configurable(self):
        s = Settings(
            pinecone_index_name="kb-staging", canonical_pinecone_index_name="kb-staging", _env_file=None
        )
        assert s.bill_filter_key == "ocd_bill_id"
        assert _settings("kb-staging").bill_filter_key == "webflow_id"


class TestJurisdictionOfNamedBill:
    def _respond(self, calls):
        def respond(f):
            if f and "gov_id" in f:
                calls.append(f)
                return [_hit("bill-text", ocd_bill_id=BILL, session_code="2026", gov_id="HB 363")]
            return []

        return respond

    async def test_page_jurisdiction_beats_a_guess_from_the_query(self):
        looked_up: list = []
        svc = _service(respond=self._respond(looked_up))
        # "Massachusetts" in the text would say MA; the page says Florida.
        await svc.retrieve(
            "How is Massachusetts doing next to HB 363?", PageContext(type="general", jurisdiction="FL")
        )
        assert looked_up[0]["jurisdiction"] == "FL"

    async def test_query_jurisdiction_is_used_when_the_page_has_none(self):
        looked_up: list = []
        svc = _service(respond=self._respond(looked_up))
        await svc.retrieve("What does Florida HB 363 do?", PageContext(type="general"))
        assert looked_up[0]["jurisdiction"] == "FL"

    @pytest.mark.parametrize("query", ["Will HB 363 actually pass?", "Summarize HB 363"])
    def test_state_codes_inside_other_words_are_not_states(self, query):
        info = _service()._extract_bill_from_query(query)
        assert info is not None and info.jurisdiction is None

    def test_a_code_that_is_also_a_word_needs_capitals(self):
        svc = _service()
        assert svc._extract_bill_from_query("Can you tell us about HB 363?").jurisdiction is None
        assert svc._extract_bill_from_query("What is US HB 363?").jurisdiction == "us"
        assert svc._extract_bill_from_query("fl hb 363").jurisdiction == "fl"  # unambiguous: unchanged

    async def test_those_queries_do_not_guess_a_state_on_a_page_without_one(self):
        looked_up: list = []
        svc = _service(respond=self._respond(looked_up))
        await svc.retrieve("Summarize HB 363", PageContext(type="general"))
        assert looked_up == []


class TestSessionAmbiguityCheck:
    async def test_the_other_session_is_seen_even_when_the_first_fills_the_old_top_10(self):
        # 20 chunks of one session rank ahead of the other session's: a top_k of 10 never saw it.
        chunks = [
            _hit("bill-text", ocd_bill_id=BILL, session_code="2026", gov_id="HB 363") for _ in range(20)
        ] + [_hit("bill-text", ocd_bill_id="other", session_code="2025", gov_id="HB 363")]
        asked: list[int] = []
        svc = _service(respond=lambda f: chunks if f and "gov_id" in f else [])
        original = svc.vector_store.query

        async def query(query, top_k=10, filter=None, include_metadata=True):
            if filter and "gov_id" in filter:
                asked.append(top_k)
                return chunks[:top_k]
            return await original(query, top_k=top_k, filter=filter)

        svc.vector_store.query = query
        result = await svc.retrieve("What does Florida HB 363 do?", PageContext(type="general"))
        assert asked == [BILL_LOOKUP_TOP_K] and BILL_LOOKUP_TOP_K > 21
        assert result.filters_applied == {}  # two bills: no guess


class TestOcdBillIdFromTheWidget:
    def test_a_valid_id_is_kept(self):
        assert _page_context_from_payload({"type": "bill", "ocd_bill_id": BILL}).ocd_bill_id == BILL

    @pytest.mark.parametrize("bad", ["not-a-uuid", "ocd-bill/" + BILL, 123, ""])
    def test_a_malformed_id_is_dropped(self, bad):
        assert _page_context_from_payload({"type": "bill", "ocd_bill_id": bad}).ocd_bill_id is None
