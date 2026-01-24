"""Tests for API schemas."""

import pytest
from pydantic import ValidationError

from votebot.api.schemas.chat import (
    ChatRequest,
    ChatResponse,
    Citation,
    PageContext,
)


class TestPageContext:
    """Tests for PageContext schema."""

    def test_valid_bill_context(self):
        """Test creating a valid bill context."""
        context = PageContext(
            type="bill",
            id="HR-1234",
            jurisdiction="US",
        )
        assert context.type == "bill"
        assert context.id == "HR-1234"
        assert context.jurisdiction == "US"

    def test_valid_legislator_context(self):
        """Test creating a valid legislator context."""
        context = PageContext(
            type="legislator",
            id="bioguide-123",
            jurisdiction="CA",
        )
        assert context.type == "legislator"

    def test_valid_general_context(self):
        """Test creating a valid general context."""
        context = PageContext(type="general")
        assert context.type == "general"
        assert context.id is None

    def test_invalid_type(self):
        """Test that invalid type raises error."""
        with pytest.raises(ValidationError):
            PageContext(type="invalid")


class TestChatRequest:
    """Tests for ChatRequest schema."""

    def test_valid_request(self, sample_bill_context):
        """Test creating a valid chat request."""
        request = ChatRequest(
            message="What does this bill do?",
            session_id="session-123",
            human_active=False,
            page_context=sample_bill_context,
        )
        assert request.message == "What does this bill do?"
        assert request.session_id == "session-123"
        assert not request.human_active

    def test_message_min_length(self, sample_bill_context):
        """Test that empty message raises error."""
        with pytest.raises(ValidationError):
            ChatRequest(
                message="",
                session_id="session-123",
                human_active=False,
                page_context=sample_bill_context,
            )

    def test_message_max_length(self, sample_bill_context):
        """Test that overly long message raises error."""
        with pytest.raises(ValidationError):
            ChatRequest(
                message="x" * 5000,
                session_id="session-123",
                human_active=False,
                page_context=sample_bill_context,
            )

    def test_human_active_default(self, sample_bill_context):
        """Test that human_active defaults to False."""
        request = ChatRequest(
            message="Test",
            session_id="session-123",
            page_context=sample_bill_context,
        )
        assert not request.human_active


class TestChatResponse:
    """Tests for ChatResponse schema."""

    def test_valid_response(self):
        """Test creating a valid chat response."""
        response = ChatResponse(
            response="This bill does XYZ.",
            citations=[],
            confidence=0.9,
        )
        assert response.response == "This bill does XYZ."
        assert response.confidence == 0.9
        assert not response.requires_human

    def test_suppressed_response(self):
        """Test creating a suppressed response."""
        response = ChatResponse(
            response=None,
            citations=[],
            confidence=0.0,
            suppressed=True,
        )
        assert response.response is None
        assert response.suppressed

    def test_response_with_citations(self):
        """Test response with citations."""
        citation = Citation(
            source="Congress.gov",
            document_id="bill-HR-1234",
            excerpt="This bill establishes...",
        )
        response = ChatResponse(
            response="The bill does this.",
            citations=[citation],
            confidence=0.85,
        )
        assert len(response.citations) == 1
        assert response.citations[0].source == "Congress.gov"


class TestCitation:
    """Tests for Citation schema."""

    def test_valid_citation(self):
        """Test creating a valid citation."""
        citation = Citation(
            source="Congress.gov",
            document_id="bill-HR-1234",
            excerpt="This is the excerpt.",
        )
        assert citation.source == "Congress.gov"
        assert citation.document_id == "bill-HR-1234"

    def test_citation_with_url(self):
        """Test citation with URL."""
        citation = Citation(
            source="Congress.gov",
            document_id="bill-HR-1234",
            excerpt="Excerpt",
            url="https://congress.gov/bill/118th-congress/house-bill/1234",
        )
        assert citation.url is not None

    def test_citation_with_relevance(self):
        """Test citation with relevance score."""
        citation = Citation(
            source="Congress.gov",
            document_id="bill-HR-1234",
            excerpt="Excerpt",
            relevance_score=0.95,
        )
        assert citation.relevance_score == 0.95
