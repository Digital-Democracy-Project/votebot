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

    def test_bill_prompt_keeps_what_changed_headings_to_the_versions_compared(self):
        """A heading must not promise a wider comparison than the sources hold (VOTEBOT-22 follow-up)."""
        info = {"id": "HB 1", "title": "T", "jurisdiction": "FL"}
        versioned = build_system_prompt(page_type="bill", page_info=info, version_aware=True)
        legacy = build_system_prompt(page_type="bill", page_info=info)

        assert 'Never write "first to latest"' in versioned
        assert "first to latest" not in legacy  # the legacy index has no version headers

    @pytest.mark.parametrize("page_type", ["bill", "legislator", "organization", "general"])
    def test_the_new_site_prompt_has_no_old_style_example_links_to_copy(self, page_type):
        # VOTEBOT-23: a model copying "/bills/education-funding-act" wrote /bills/hb-5601e, which the new site cannot open
        prompt = build_system_prompt(page_type=page_type, page_info={"id": "HB 1", "title": "T"}, version_aware=True)

        assert "digitaldemocracyproject.org/bills/" not in prompt
        assert "digitaldemocracyproject.org/legislators/" not in prompt
        assert "Education Funding Act" not in prompt
        assert "Never write any other bill URL and never build one from a bill's name or number" in prompt
        assert "https://digitaldemocracyproject.org/vote" in prompt  # the sign-up link is still there
        assert prompt.count("## Linking to Bills and Legislators") == 1  # the section was changed, not duplicated

    def test_the_link_examples_appear_once_in_the_base_prompt_and_the_swap_replaces_exactly_them(self):
        from votebot.core.prompts import CANONICAL_LINK_RULE, LEGACY_LINK_EXAMPLES

        assert SYSTEM_PROMPT_BASE.count(LEGACY_LINK_EXAMPLES) == 1
        prompt = build_system_prompt(page_type="general", version_aware=True)
        assert prompt.count(CANONICAL_LINK_RULE) == 1
        assert SYSTEM_PROMPT_BASE.replace(LEGACY_LINK_EXAMPLES, CANONICAL_LINK_RULE) in prompt

    def test_a_canonical_bill_chunk_shows_our_page_as_its_source_url_and_no_old_style_ddp_url(self):
        # what "Source URL" means on the new index (the rule allows it only for a source about the same bill):
        # the chunk's own url, which retrieval has already mapped to our page; no slug, so no old-style "DDP URL"
        chunk = {
            "id": "bill-text:a3f7:1-chunk-0",
            "content": "text",
            "metadata": {
                "source": "OpenStates archive", "document_type": "bill-text", "gov_id": "HB 5601E",
                "url": "https://dev.digitaldemocracyproject.org/explore/FL/2026E/HB%205601E",
                "source_url": "https://www.flsenate.gov/x.pdf",
            },
        }
        text = format_retrieved_chunks([chunk])

        assert "**Source URL:** https://dev.digitaldemocracyproject.org/explore/FL/2026E/HB%205601E" in text
        assert "DDP URL" not in text
        assert "/bills/" not in text

    def test_the_legacy_prompt_keeps_its_examples_unchanged(self):
        prompt = build_system_prompt(page_type="bill", page_info={"id": "HB 1", "title": "T"})

        assert SYSTEM_PROMPT_BASE in prompt
        assert "digitaldemocracyproject.org/bills/education-funding-act" in prompt
        assert "Never write any other bill URL" not in prompt

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
