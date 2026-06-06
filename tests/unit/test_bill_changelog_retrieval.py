"""Tests for Phase 2 bill changelog retrieval.

Covers PLAN-bill-version-history.md Phase 2:
- Changelog sub-intent detection in intent.py
- Phase 5 changelog retrieval in retrieval.py
- Changelog chunk header formatting in prompts.py
- Retrieval isolation (history/changelog invisible to non-changelog queries)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from votebot.api.schemas.chat import PageContext
from votebot.core.prompts import format_retrieved_chunks
from votebot.utils.intent import (
    VALID_RETRIEVAL_SOURCES,
    SubIntent,
    classify_sub_intent,
    PrimaryIntent,
)


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

class TestChangelogSubIntent:
    def test_what_changed_triggers_changelog(self):
        assert classify_sub_intent(PrimaryIntent.BILL, "what changed in this bill?") == SubIntent.CHANGELOG

    def test_what_has_changed_triggers_changelog(self):
        assert classify_sub_intent(PrimaryIntent.BILL, "what has changed since it was introduced?") == SubIntent.CHANGELOG

    def test_new_version_triggers_changelog(self):
        assert classify_sub_intent(PrimaryIntent.BILL, "what's new in the new version?") == SubIntent.CHANGELOG

    def test_what_was_added_triggers_changelog(self):
        assert classify_sub_intent(PrimaryIntent.BILL, "what was added to this bill?") == SubIntent.CHANGELOG

    def test_what_was_removed_triggers_changelog(self):
        assert classify_sub_intent(PrimaryIntent.BILL, "what was removed from the bill?") == SubIntent.CHANGELOG

    def test_normal_status_query_does_not_trigger_changelog(self):
        result = classify_sub_intent(PrimaryIntent.BILL, "what is the current status of this bill?")
        assert result != SubIntent.CHANGELOG

    def test_vote_query_does_not_trigger_changelog(self):
        result = classify_sub_intent(PrimaryIntent.BILL, "how did senators vote on this?")
        assert result != SubIntent.CHANGELOG

    def test_difference_triggers_changelog(self):
        assert classify_sub_intent(PrimaryIntent.BILL, "what's the difference between the versions?") == SubIntent.CHANGELOG

    def test_changelog_is_valid_sub_intent(self):
        assert SubIntent.CHANGELOG == "changelog"

    def test_status_query_with_amendment_word_does_not_trigger_changelog(self):
        """'amended' alone in a status question should not be classified as changelog."""
        result = classify_sub_intent(PrimaryIntent.BILL, "was this bill amended by the committee?")
        # "amended" is not in the intent list (only in retrieval), so falls through to "status"
        assert result != SubIntent.CHANGELOG

    def test_non_bill_context_vote_query_does_not_trigger_changelog(self):
        """Changelog sub-intent only applies to bill primary intent."""
        result = classify_sub_intent(PrimaryIntent.LEGISLATOR, "what changed in her voting record?")
        assert result != SubIntent.CHANGELOG


# ---------------------------------------------------------------------------
# VALID_RETRIEVAL_SOURCES
# ---------------------------------------------------------------------------

class TestValidRetrievalSources:
    def test_bill_changelog_is_valid(self):
        assert "bill-changelog" in VALID_RETRIEVAL_SOURCES

    def test_bill_text_history_is_valid(self):
        assert "bill-text-history" in VALID_RETRIEVAL_SOURCES


# ---------------------------------------------------------------------------
# Retrieval phase 5
# ---------------------------------------------------------------------------

def _make_search_result(doc_type: str, score: float = 0.85, content: str = "chunk content") -> MagicMock:
    r = MagicMock()
    r.id = f"{doc_type}-chunk-0"
    r.content = content
    r.score = score
    r.metadata = {"document_type": doc_type, "webflow_id": "webflow123"}
    return r


class TestChangelogRetrieval:
    """Phase 5 retrieval fires on changelog queries and not on others."""

    def _make_service(self):
        from votebot.core.retrieval import RetrievalService
        svc = RetrievalService.__new__(RetrievalService)
        svc.settings = MagicMock()
        svc.config = MagicMock()
        svc.config.max_chunks = 10
        svc.config.similarity_threshold = 0.7
        svc.config.deduplicate = False
        return svc

    def _make_page_context(self, webflow_id: str = "webflow123") -> PageContext:
        return PageContext(
            type="bill",
            webflow_id=webflow_id,
            slug="test-bill-2026",
            title="Test Bill",
        )

    async def test_changelog_phase_fires_on_changelog_query(self):
        svc = self._make_service()
        changelog_chunk = _make_search_result("bill-changelog")
        text_chunk = _make_search_result("bill-text")

        call_count = {"n": 0}
        async def mock_query(query, top_k, filter):
            call_count["n"] += 1
            dt = filter.get("document_type", "")
            if dt == "bill-changelog":
                return [changelog_chunk]
            elif dt == "bill-text":
                return [text_chunk]
            return []

        svc.vector_store = MagicMock()
        svc.vector_store.query = mock_query

        result = await svc._retrieve_bill_with_text_priority(
            query="what changed in this bill?",
            filters={"webflow_id": "webflow123"},
            max_chunks=10,
            page_context=self._make_page_context(),
        )

        doc_types = [r.metadata["document_type"] for r in result]
        assert "bill-changelog" in doc_types
        # Changelog appears first
        assert doc_types[0] == "bill-changelog"

    async def test_changelog_phase_does_not_fire_on_normal_query(self):
        svc = self._make_service()
        text_chunk = _make_search_result("bill-text")

        queried_types = []
        async def mock_query(query, top_k, filter):
            dt = filter.get("document_type", "")
            queried_types.append(dt)
            if dt == "bill-text":
                return [text_chunk]
            return []

        svc.vector_store = MagicMock()
        svc.vector_store.query = mock_query

        await svc._retrieve_bill_with_text_priority(
            query="what does this bill do?",
            filters={"webflow_id": "webflow123"},
            max_chunks=10,
            page_context=self._make_page_context(),
        )

        assert "bill-changelog" not in queried_types

    async def test_changelog_phase_skipped_without_webflow_id(self):
        """No webflow_id in filters → changelog phase does not fire."""
        svc = self._make_service()
        queried_types = []

        async def mock_query(query, top_k, filter=None):
            queried_types.append((filter or {}).get("document_type", ""))
            return []

        svc.vector_store = MagicMock()
        svc.vector_store.query = mock_query

        await svc._retrieve_bill_with_text_priority(
            query="what changed in this bill?",
            filters={},  # no webflow_id
            max_chunks=10,
            page_context=self._make_page_context(webflow_id=""),
        )

        assert "bill-changelog" not in queried_types

    async def test_bill_text_history_never_retrieved(self):
        """bill-text-history is never queried by any retrieval path."""
        svc = self._make_service()
        queried_types = []

        async def mock_query(query, top_k, filter):
            queried_types.append(filter.get("document_type", ""))
            return []

        svc.vector_store = MagicMock()
        svc.vector_store.query = mock_query

        # Even on a changelog query, bill-text-history should never be queried
        await svc._retrieve_bill_with_text_priority(
            query="what changed?",
            filters={"webflow_id": "webflow123"},
            max_chunks=10,
            page_context=self._make_page_context(),
        )

        assert "bill-text-history" not in queried_types

    async def test_below_threshold_changelog_chunks_excluded(self):
        """Changelog chunks below similarity_threshold are filtered out."""
        svc = self._make_service()
        svc.config.similarity_threshold = 0.7
        low_score_chunk = _make_search_result("bill-changelog", score=0.5)

        async def mock_query(query, top_k, filter):
            if filter.get("document_type") == "bill-changelog":
                return [low_score_chunk]
            return []

        svc.vector_store = MagicMock()
        svc.vector_store.query = mock_query

        result = await svc._retrieve_bill_with_text_priority(
            query="what changed in this bill?",
            filters={"webflow_id": "webflow123"},
            max_chunks=10,
            page_context=self._make_page_context(),
        )

        doc_types = [r.metadata["document_type"] for r in result]
        assert "bill-changelog" not in doc_types


# ---------------------------------------------------------------------------
# Prompt formatting
# ---------------------------------------------------------------------------

class TestChangelogChunkFormatting:
    def _make_chunk(self, doc_type: str, extra_meta: dict | None = None) -> dict:
        meta = {"document_type": doc_type, "source": "Digital Democracy Project"}
        if extra_meta:
            meta.update(extra_meta)
        return {"id": f"{doc_type}-chunk-0", "content": "Changelog content here.", "metadata": meta}

    def test_changelog_chunk_shows_version_transition(self):
        chunk = self._make_chunk("bill-changelog", {
            "version_from_note": "Introduced in Senate",
            "version_from_date": "2026-03-01",
            "version_to_note": "Placed on Calendar Senate",
            "version_to_date": "2026-05-20",
        })
        formatted = format_retrieved_chunks([chunk])
        assert "Version Change:" in formatted
        assert "Introduced in Senate" in formatted
        assert "Placed on Calendar Senate" in formatted
        assert "→" in formatted

    def test_changelog_chunk_handles_missing_dates(self):
        chunk = self._make_chunk("bill-changelog", {
            "version_from_note": "Introduced",
            "version_to_note": "Engrossed",
        })
        formatted = format_retrieved_chunks([chunk])
        assert "Version Change:" in formatted
        assert "Introduced" in formatted
        assert "Engrossed" in formatted

    def test_non_changelog_chunk_has_no_version_transition(self):
        chunk = self._make_chunk("bill-text")
        formatted = format_retrieved_chunks([chunk])
        assert "Version Change:" not in formatted

    def test_bill_summary_chunk_has_no_version_transition(self):
        chunk = self._make_chunk("bill")
        formatted = format_retrieved_chunks([chunk])
        assert "Version Change:" not in formatted
