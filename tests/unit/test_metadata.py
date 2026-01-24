"""Tests for metadata extraction."""

import pytest

from votebot.ingestion.metadata import DocumentMetadata, MetadataExtractor


class TestDocumentMetadata:
    """Tests for DocumentMetadata class."""

    def test_create_metadata(self):
        """Test creating document metadata."""
        metadata = DocumentMetadata(
            document_id="test-123",
            document_type="bill",
            source="congress.gov",
            title="Test Bill",
            jurisdiction="US",
            bill_id="HR-1234",
        )

        assert metadata.document_id == "test-123"
        assert metadata.document_type == "bill"
        assert metadata.source == "congress.gov"

    def test_to_dict(self):
        """Test converting metadata to dict."""
        metadata = DocumentMetadata(
            document_id="test-123",
            document_type="bill",
            source="congress.gov",
            title="Test Bill",
            jurisdiction="US",
        )

        result = metadata.to_dict()

        assert result["document_id"] == "test-123"
        assert result["document_type"] == "bill"
        assert result["title"] == "Test Bill"
        assert "created_at" in result

    def test_to_dict_excludes_none(self):
        """Test that None values are excluded from dict."""
        metadata = DocumentMetadata(
            document_id="test-123",
            document_type="bill",
            source="test",
        )

        result = metadata.to_dict()

        assert "bill_id" not in result
        assert "legislator_id" not in result


class TestMetadataExtractor:
    """Tests for MetadataExtractor class."""

    @pytest.fixture
    def extractor(self):
        """Create a metadata extractor instance."""
        return MetadataExtractor()

    def test_extract_congress_bill_metadata(self, extractor):
        """Test extracting metadata from Congress.gov bill data."""
        raw_data = {
            "number": "1234",
            "type": "hr",
            "congress": "118",
            "title": "Clean Energy Act",
            "introducedDate": "2024-01-15",
            "sponsor": {"name": "Rep. Jane Smith"},
            "latestAction": {"text": "Passed House"},
        }

        metadata = extractor.extract_bill_metadata(raw_data, "congress.gov")

        assert metadata.document_type == "bill"
        assert metadata.source == "congress.gov"
        assert "hr1234" in metadata.bill_id.lower()
        assert metadata.jurisdiction == "US"

    def test_extract_openstates_bill_metadata(self, extractor):
        """Test extracting metadata from OpenStates bill data."""
        raw_data = {
            "identifier": "SB 5678",
            "title": "Housing Act",
            "jurisdiction": {
                "name": "California",
                "classification": "CA",
            },
        }

        metadata = extractor.extract_bill_metadata(raw_data, "openstates")

        assert metadata.document_type == "bill"
        assert metadata.source == "openstates"
        assert "SB 5678" in metadata.bill_id

    def test_extract_legislator_metadata(self, extractor):
        """Test extracting legislator metadata."""
        raw_data = {
            "id": "bioguide-123",
            "name": "Rep. Jane Smith",
            "party": "D",
            "state": "CA",
            "chamber": "House",
        }

        metadata = extractor.extract_legislator_metadata(raw_data, "congress.gov")

        assert metadata.document_type == "legislator"
        assert metadata.legislator_id == "bioguide-123"
        assert "Jane Smith" in metadata.title

    def test_extract_web_content_metadata(self, extractor):
        """Test extracting web content metadata."""
        metadata = extractor.extract_web_content_metadata(
            url="https://example.com/page",
            title="Test Page",
            content_type="article",
        )

        assert metadata.document_type == "article"
        assert metadata.source == "web"
        assert metadata.url == "https://example.com/page"

    def test_normalize_jurisdiction(self, extractor):
        """Test jurisdiction normalization."""
        assert extractor.normalize_jurisdiction("federal") == "US"
        assert extractor.normalize_jurisdiction("united states") == "US"
        assert extractor.normalize_jurisdiction("CA") == "CA"
        assert extractor.normalize_jurisdiction("ca") == "CA"
