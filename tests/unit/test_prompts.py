"""Tests for prompt building."""

import pytest

from votebot.core.prompts import (
    build_system_prompt,
    format_retrieved_chunks,
    SYSTEM_PROMPT_BASE,
)


class TestBuildSystemPrompt:
    """Tests for build_system_prompt function."""

    def test_general_context_prompt(self):
        """Test prompt for general context."""
        prompt = build_system_prompt(page_type="general")

        assert SYSTEM_PROMPT_BASE in prompt
        assert "General Browsing" in prompt

    def test_bill_context_prompt(self):
        """Test prompt for bill context."""
        page_info = {
            "id": "HR-1234",
            "title": "Clean Energy Act",
            "jurisdiction": "US",
        }
        prompt = build_system_prompt(
            page_type="bill",
            page_info=page_info,
        )

        assert "Bill Page" in prompt
        assert "HR-1234" in prompt
        assert "Clean Energy Act" in prompt

    def test_legislator_context_prompt(self):
        """Test prompt for legislator context."""
        page_info = {
            "id": "bioguide-123",
            "name": "Rep. Jane Smith",
            "party": "D",
            "state": "CA",
        }
        prompt = build_system_prompt(
            page_type="legislator",
            page_info=page_info,
        )

        assert "Legislator Page" in prompt
        assert "Rep. Jane Smith" in prompt

    def test_prompt_includes_rag_context(self):
        """Test that RAG context is included when provided."""
        retrieved_context = "This is retrieved content about the bill."
        prompt = build_system_prompt(
            page_type="bill",
            include_rag_context=True,
            retrieved_context=retrieved_context,
        )

        assert "Retrieved Information" in prompt
        assert retrieved_context in prompt

    def test_prompt_excludes_rag_context_when_disabled(self):
        """Test that RAG context is excluded when disabled."""
        retrieved_context = "This should not appear."
        prompt = build_system_prompt(
            page_type="bill",
            include_rag_context=False,
            retrieved_context=retrieved_context,
        )

        assert retrieved_context not in prompt

    def test_prompt_includes_citation_instructions(self):
        """Test that citation instructions are included."""
        prompt = build_system_prompt(page_type="general")

        assert "[Source:" in prompt

    def test_prompt_includes_confidence_scoring(self):
        """Test that confidence scoring guidance is included."""
        prompt = build_system_prompt(page_type="general")

        assert "Confidence Scoring" in prompt


class TestFormatRetrievedChunks:
    """Tests for format_retrieved_chunks function."""

    def test_format_single_chunk(self):
        """Test formatting a single chunk."""
        chunks = [
            {
                "id": "doc-1",
                "content": "This is the content.",
                "metadata": {"source": "Congress.gov"},
            }
        ]
        formatted = format_retrieved_chunks(chunks)

        assert "Source 1" in formatted
        assert "Congress.gov" in formatted
        assert "This is the content." in formatted

    def test_format_multiple_chunks(self):
        """Test formatting multiple chunks."""
        chunks = [
            {
                "id": "doc-1",
                "content": "First content.",
                "metadata": {"source": "Congress.gov"},
            },
            {
                "id": "doc-2",
                "content": "Second content.",
                "metadata": {"source": "OpenStates"},
            },
        ]
        formatted = format_retrieved_chunks(chunks)

        assert "Source 1" in formatted
        assert "Source 2" in formatted
        assert "First content." in formatted
        assert "Second content." in formatted

    def test_format_empty_chunks(self):
        """Test formatting empty chunk list."""
        formatted = format_retrieved_chunks([])

        assert "No relevant documents found" in formatted

    def test_format_chunk_with_missing_metadata(self):
        """Test formatting chunk with missing metadata."""
        chunks = [
            {
                "id": "doc-1",
                "content": "Content here.",
            }
        ]
        formatted = format_retrieved_chunks(chunks)

        assert "Unknown" in formatted
        assert "Content here." in formatted
