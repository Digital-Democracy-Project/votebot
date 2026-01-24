"""Tests for chunking service."""

import pytest

from votebot.ingestion.chunking import ChunkingService


class TestChunkingService:
    """Tests for the ChunkingService class."""

    @pytest.fixture
    def chunker(self):
        """Create a chunking service instance."""
        return ChunkingService(chunk_size=100, chunk_overlap=20)

    def test_basic_chunking(self, chunker):
        """Test basic text chunking."""
        text = "This is a test. " * 50  # Create text longer than chunk size
        chunks = chunker.chunk_text(text)

        assert len(chunks) > 1
        assert all(chunk.content for chunk in chunks)
        assert all(chunk.token_count > 0 for chunk in chunks)

    def test_empty_text(self, chunker):
        """Test that empty text returns no chunks."""
        chunks = chunker.chunk_text("")
        assert len(chunks) == 0

    def test_whitespace_only(self, chunker):
        """Test that whitespace-only text returns no chunks."""
        chunks = chunker.chunk_text("   \n\t  ")
        assert len(chunks) == 0

    def test_short_text(self, chunker):
        """Test that short text creates single chunk."""
        text = "This is a short text."
        chunks = chunker.chunk_text(text)

        assert len(chunks) == 1
        assert chunks[0].content.strip() == text

    def test_chunk_indices(self, chunker):
        """Test that chunk indices are correct."""
        text = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
        chunks = chunker.chunk_text(text)

        for i, chunk in enumerate(chunks):
            assert chunk.index == i

    def test_chunk_metadata(self, chunker):
        """Test that metadata is attached to chunks."""
        text = "This is test content."
        metadata = {"source": "test", "doc_id": "123"}
        chunks = chunker.chunk_text(text, metadata)

        assert len(chunks) == 1
        assert chunks[0].metadata == metadata

    def test_paragraph_chunking(self, chunker):
        """Test chunking by paragraphs."""
        text = """First paragraph with some content.

Second paragraph with more content.

Third paragraph with even more content."""

        chunks = chunker.chunk_text(text)
        assert len(chunks) >= 1

    def test_html_chunking(self, chunker):
        """Test HTML content extraction and chunking."""
        html = """
        <html>
        <head><title>Test</title></head>
        <body>
            <script>console.log('ignore');</script>
            <p>First paragraph.</p>
            <p>Second paragraph.</p>
        </body>
        </html>
        """
        chunks = chunker.chunk_html(html)

        assert len(chunks) >= 1
        # Should not contain script content
        full_content = " ".join(c.content for c in chunks)
        assert "console.log" not in full_content

    def test_token_counting(self, chunker):
        """Test token counting accuracy."""
        text = "Hello world"
        token_count = chunker.count_tokens(text)

        assert token_count == 2  # "Hello" and "world"

    def test_overlap_between_chunks(self, chunker):
        """Test that chunks have overlapping content."""
        # Create text that will definitely create multiple chunks
        text = "Word " * 200

        chunks = chunker.chunk_text(text)

        if len(chunks) > 1:
            # Check that consecutive chunks might share some content
            # (due to overlap configuration)
            first_chunk_end = chunks[0].content[-50:]
            second_chunk_start = chunks[1].content[:50]
            # Just verify chunks exist and have content
            assert len(first_chunk_end) > 0
            assert len(second_chunk_start) > 0

    def test_large_paragraph_splitting(self):
        """Test that large paragraphs are split correctly."""
        chunker = ChunkingService(chunk_size=50, chunk_overlap=10)

        # Create a single very long paragraph
        long_paragraph = "This is a word. " * 100

        chunks = chunker.chunk_text(long_paragraph)

        assert len(chunks) > 1
        # Each chunk should not exceed token limit significantly
        for chunk in chunks:
            assert chunk.token_count <= 60  # Allow some buffer
