"""Text chunking service for document processing."""

import re
from dataclasses import dataclass
from typing import Callable

import structlog
import tiktoken

logger = structlog.get_logger()


@dataclass
class Chunk:
    """A text chunk with metadata."""

    content: str
    index: int
    token_count: int
    start_char: int
    end_char: int
    metadata: dict | None = None


class ChunkingService:
    """
    Service for chunking text into appropriately sized pieces.

    Supports:
    - Token-aware chunking
    - Overlap handling for context preservation
    - Multiple content types (HTML, plain text, PDF)
    - Semantic chunking (by paragraph/section)
    """

    def __init__(
        self,
        chunk_size: int = 750,
        chunk_overlap: int = 150,
        model: str = "cl100k_base",
    ):
        """
        Initialize the chunking service.

        Args:
            chunk_size: Target size of each chunk in tokens
            chunk_overlap: Number of overlapping tokens between chunks
            model: Tiktoken model for tokenization
        """
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.encoding = tiktoken.get_encoding(model)

    def chunk_text(
        self,
        text: str,
        metadata: dict | None = None,
    ) -> list[Chunk]:
        """
        Chunk text into smaller pieces.

        Args:
            text: The text to chunk
            metadata: Optional metadata to attach to chunks

        Returns:
            List of Chunk objects
        """
        if not text or not text.strip():
            return []

        # Clean and normalize text
        text = self._clean_text(text)

        # Try semantic chunking first (by paragraphs)
        paragraphs = self._split_into_paragraphs(text)

        if paragraphs:
            return self._chunk_by_paragraphs(paragraphs, metadata)

        # Fall back to token-based chunking
        return self._chunk_by_tokens(text, metadata)

    def chunk_html(
        self,
        html: str,
        metadata: dict | None = None,
    ) -> list[Chunk]:
        """
        Chunk HTML content after extracting text.

        Args:
            html: HTML content to chunk
            metadata: Optional metadata to attach to chunks

        Returns:
            List of Chunk objects
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")

        # Remove script and style elements
        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.decompose()

        # Extract text
        text = soup.get_text(separator="\n\n")

        return self.chunk_text(text, metadata)

    def chunk_pdf_text(
        self,
        text: str,
        metadata: dict | None = None,
    ) -> list[Chunk]:
        """
        Chunk PDF-extracted text.

        Args:
            text: Text extracted from PDF
            metadata: Optional metadata to attach to chunks

        Returns:
            List of Chunk objects
        """
        # PDF text often has different formatting
        # Normalize line breaks
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)

        return self.chunk_text(text, metadata)

    def _clean_text(self, text: str) -> str:
        """Clean and normalize text."""
        # Normalize whitespace
        text = re.sub(r"\s+", " ", text)
        # Remove excessive newlines
        text = re.sub(r"\n{3,}", "\n\n", text)
        # Strip leading/trailing whitespace
        text = text.strip()
        return text

    def _split_into_paragraphs(self, text: str) -> list[str]:
        """Split text into paragraphs."""
        # Split on double newlines or multiple spaces indicating paragraph breaks
        paragraphs = re.split(r"\n\n+|\n\s*\n", text)
        return [p.strip() for p in paragraphs if p.strip()]

    def _chunk_by_paragraphs(
        self,
        paragraphs: list[str],
        metadata: dict | None,
    ) -> list[Chunk]:
        """Chunk by combining paragraphs up to chunk_size."""
        chunks = []
        current_chunk = []
        current_tokens = 0
        current_start = 0
        char_position = 0

        for paragraph in paragraphs:
            para_tokens = len(self.encoding.encode(paragraph))

            # If single paragraph exceeds chunk size, split it
            if para_tokens > self.chunk_size:
                # Flush current chunk
                if current_chunk:
                    content = "\n\n".join(current_chunk)
                    chunks.append(
                        Chunk(
                            content=content,
                            index=len(chunks),
                            token_count=current_tokens,
                            start_char=current_start,
                            end_char=char_position,
                            metadata=metadata,
                        )
                    )
                    current_chunk = []
                    current_tokens = 0
                    current_start = char_position

                # Split the large paragraph
                sub_chunks = self._chunk_by_tokens(paragraph, metadata)
                for sub_chunk in sub_chunks:
                    sub_chunk.index = len(chunks)
                    sub_chunk.start_char += char_position
                    sub_chunk.end_char += char_position
                    chunks.append(sub_chunk)

                char_position += len(paragraph) + 2
                current_start = char_position
                continue

            # Check if adding this paragraph exceeds chunk size
            if current_tokens + para_tokens > self.chunk_size and current_chunk:
                # Create chunk from current content
                content = "\n\n".join(current_chunk)
                chunks.append(
                    Chunk(
                        content=content,
                        index=len(chunks),
                        token_count=current_tokens,
                        start_char=current_start,
                        end_char=char_position,
                        metadata=metadata,
                    )
                )

                # Handle overlap by keeping last paragraph(s)
                overlap_content = self._get_overlap_content(current_chunk)
                current_chunk = [overlap_content] if overlap_content else []
                current_tokens = (
                    len(self.encoding.encode(overlap_content)) if overlap_content else 0
                )
                current_start = char_position

            current_chunk.append(paragraph)
            current_tokens += para_tokens
            char_position += len(paragraph) + 2

        # Don't forget the last chunk
        if current_chunk:
            content = "\n\n".join(current_chunk)
            chunks.append(
                Chunk(
                    content=content,
                    index=len(chunks),
                    token_count=current_tokens,
                    start_char=current_start,
                    end_char=char_position,
                    metadata=metadata,
                )
            )

        return chunks

    def _chunk_by_tokens(
        self,
        text: str,
        metadata: dict | None,
    ) -> list[Chunk]:
        """Chunk text based on token count."""
        tokens = self.encoding.encode(text)
        chunks = []
        start = 0

        while start < len(tokens):
            # Calculate end position
            end = min(start + self.chunk_size, len(tokens))

            # Decode the chunk
            chunk_tokens = tokens[start:end]
            content = self.encoding.decode(chunk_tokens)

            # Calculate character positions
            start_char = len(self.encoding.decode(tokens[:start]))
            end_char = len(self.encoding.decode(tokens[:end]))

            chunks.append(
                Chunk(
                    content=content,
                    index=len(chunks),
                    token_count=len(chunk_tokens),
                    start_char=start_char,
                    end_char=end_char,
                    metadata=metadata,
                )
            )

            # If we've reached the end, break
            if end >= len(tokens):
                break

            # Move start with overlap, ensuring we make progress
            new_start = end - self.chunk_overlap
            if new_start <= start:
                # Ensure we always make progress
                new_start = start + 1
            start = new_start

        return chunks

    def _get_overlap_content(self, paragraphs: list[str]) -> str:
        """Get content for overlap from previous chunk."""
        if not paragraphs:
            return ""

        # Take last paragraph if it's small enough
        last_para = paragraphs[-1]
        if len(self.encoding.encode(last_para)) <= self.chunk_overlap:
            return last_para

        # Otherwise truncate
        tokens = self.encoding.encode(last_para)
        return self.encoding.decode(tokens[-self.chunk_overlap :])

    def count_tokens(self, text: str) -> int:
        """Count the number of tokens in text."""
        return len(self.encoding.encode(text))


class ChunkingServiceFactory:
    """Factory for creating chunking service instances."""

    @staticmethod
    def create(
        chunk_size: int = 750,
        chunk_overlap: int = 150,
    ) -> ChunkingService:
        """Create a chunking service with the specified settings."""
        return ChunkingService(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
