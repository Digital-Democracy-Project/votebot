"""Integration tests for the ingestion pipeline."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from votebot.ingestion.metadata import DocumentMetadata
from votebot.ingestion.pipeline import DocumentSource, IngestionPipeline


class TestIngestionPipeline:
    """Integration tests for IngestionPipeline."""

    @pytest.fixture
    def mock_vector_store(self):
        """Mock vector store service."""
        with patch("votebot.ingestion.pipeline.VectorStoreService") as mock:
            mock_instance = MagicMock()
            mock_instance.upsert_documents = AsyncMock(return_value=5)
            mock.return_value = mock_instance
            yield mock_instance

    @pytest.fixture
    def pipeline(self, settings, mock_vector_store):
        """Create a pipeline with mocked dependencies."""
        return IngestionPipeline(settings)

    @pytest.mark.asyncio
    async def test_ingest_single_document(self, pipeline, mock_vector_store):
        """Test ingesting a single document."""
        metadata = DocumentMetadata(
            document_id="test-doc-1",
            document_type="bill",
            source="test",
            title="Test Bill",
        )

        content = "This is the content of the test bill. " * 20

        result = await pipeline.ingest_document(content, metadata)

        assert result.documents_processed == 1
        assert result.chunks_created > 0
        assert len(result.errors) == 0

    @pytest.mark.asyncio
    async def test_ingest_batch(self, pipeline, mock_vector_store):
        """Test batch ingestion."""
        documents = []
        for i in range(3):
            metadata = DocumentMetadata(
                document_id=f"test-doc-{i}",
                document_type="bill",
                source="test",
            )
            documents.append(
                DocumentSource(
                    content=f"Content for document {i}. " * 20,
                    metadata=metadata,
                )
            )

        result = await pipeline.ingest_batch(documents)

        assert result.documents_processed == 3
        assert len(result.errors) == 0

    @pytest.mark.asyncio
    async def test_duplicate_detection(self, pipeline, mock_vector_store):
        """Test that duplicate content is detected."""
        metadata = DocumentMetadata(
            document_id="test-doc-1",
            document_type="bill",
            source="test",
        )

        content = "This is duplicate content."

        # First ingestion
        result1 = await pipeline.ingest_document(content, metadata)
        assert result1.documents_processed == 1

        # Second ingestion with same content
        result2 = await pipeline.ingest_document(content, metadata)
        assert result2.skipped == 1
        assert result2.documents_processed == 0

    @pytest.mark.asyncio
    async def test_empty_content_handling(self, pipeline, mock_vector_store):
        """Test handling of empty content."""
        metadata = DocumentMetadata(
            document_id="test-doc-1",
            document_type="bill",
            source="test",
        )

        result = await pipeline.ingest_document("", metadata)

        # Document is processed but no chunks are created from empty content
        assert result.documents_processed == 1
        assert result.chunks_created == 0
        assert result.chunks_upserted == 0
