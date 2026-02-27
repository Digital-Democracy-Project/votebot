"""PDF document processor and data source."""

import os
from pathlib import Path
from typing import AsyncIterator

import structlog

from votebot.config import Settings, get_settings
from votebot.ingestion.metadata import MetadataExtractor
from votebot.ingestion.pipeline import DocumentSource

logger = structlog.get_logger()


class PDFSource:
    """
    Data source for PDF documents.

    Processes:
    - Grant documents
    - Policy papers
    - Legislative analysis reports
    - Any PDF document
    """

    def __init__(
        self,
        settings: Settings | None = None,
        metadata_extractor: MetadataExtractor | None = None,
    ):
        """
        Initialize the PDF source.

        Args:
            settings: Application settings
            metadata_extractor: Metadata extractor instance
        """
        self.settings = settings or get_settings()
        self.metadata_extractor = metadata_extractor or MetadataExtractor()

    async def fetch(
        self,
        directory: str | None = None,
        files: list[str] | None = None,
        recursive: bool = True,
        **kwargs,
    ) -> AsyncIterator[DocumentSource]:
        """
        Process PDF files from a directory or list.

        Args:
            directory: Directory containing PDF files
            files: List of specific file paths
            recursive: Whether to search subdirectories

        Yields:
            DocumentSource objects for each PDF
        """
        # Collect PDF files
        pdf_files = []

        if files:
            pdf_files.extend(files)

        if directory:
            dir_path = Path(directory)
            if recursive:
                pdf_files.extend(str(p) for p in dir_path.rglob("*.pdf"))
            else:
                pdf_files.extend(str(p) for p in dir_path.glob("*.pdf"))

        logger.info(f"Found {len(pdf_files)} PDF files to process")

        for file_path in pdf_files:
            try:
                doc = await self.process_file(file_path)
                if doc:
                    yield doc
            except Exception as e:
                logger.warning(
                    "Failed to process PDF",
                    file=file_path,
                    error=str(e),
                )
                continue

    async def process_file(self, file_path: str) -> DocumentSource | None:
        """
        Process a single PDF file.

        Args:
            file_path: Path to the PDF file

        Returns:
            DocumentSource or None if processing failed
        """
        if not os.path.exists(file_path):
            logger.warning("PDF file not found", file=file_path)
            return None

        logger.info("Processing PDF", file=file_path)

        # Extract text using pdfplumber (primary) or PyPDF2 (fallback)
        text, pdf_metadata = await self._extract_text(file_path)

        if not text or len(text.strip()) < 100:
            logger.warning("Insufficient text extracted from PDF", file=file_path)
            return None

        # Extract metadata
        metadata = self.metadata_extractor.extract_pdf_metadata(
            file_path=file_path,
            pdf_metadata=pdf_metadata,
        )

        return DocumentSource(
            content=text,
            metadata=metadata,
        )

    async def _extract_text(self, file_path: str) -> tuple[str, dict | None]:
        """
        Extract text from a PDF file.

        Tries pdfplumber first, falls back to PyPDF2.

        Args:
            file_path: Path to the PDF file

        Returns:
            Tuple of (extracted_text, pdf_metadata)
        """
        # Try pdfplumber first (better for complex layouts)
        try:
            import pdfplumber

            text_parts = []
            metadata = None

            with pdfplumber.open(file_path) as pdf:
                metadata = pdf.metadata

                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)

            if text_parts:
                return "\n\n".join(text_parts), metadata

        except ImportError:
            logger.debug("pdfplumber not available, trying PyPDF2")
        except Exception as e:
            logger.warning(f"pdfplumber extraction failed: {e}")

        # Fallback to PyPDF2
        try:
            from PyPDF2 import PdfReader

            reader = PdfReader(file_path)
            metadata = reader.metadata

            text_parts = []
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)

            return "\n\n".join(text_parts), metadata

        except Exception as e:
            logger.error(f"PyPDF2 extraction failed: {e}")
            return "", None

    # Skip PDFs larger than this to avoid OOM during batch sync
    MAX_PDF_BYTES = 15 * 1024 * 1024  # 15 MB

    async def process_url(self, url: str, save_path: str | None = None) -> DocumentSource | None:
        """
        Download and process a PDF from a URL.

        Args:
            url: URL of the PDF
            save_path: Optional path to save the downloaded PDF

        Returns:
            DocumentSource or None if processing failed
        """
        import tempfile

        import httpx

        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            # Stream the download to disk to avoid holding the full PDF in memory
            try:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()

                    # Check Content-Length before downloading
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > self.MAX_PDF_BYTES:
                        logger.warning(
                            "Skipping oversized PDF",
                            url=url,
                            size_mb=round(int(content_length) / 1024 / 1024, 1),
                            limit_mb=self.MAX_PDF_BYTES // 1024 // 1024,
                        )
                        return None

                    # Save to file
                    if save_path:
                        file_path = save_path
                    else:
                        fd, file_path = tempfile.mkstemp(suffix=".pdf")
                        os.close(fd)

                    bytes_written = 0
                    with open(file_path, "wb") as f:
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            bytes_written += len(chunk)
                            if bytes_written > self.MAX_PDF_BYTES:
                                logger.warning(
                                    "PDF exceeded size limit during download, truncating",
                                    url=url,
                                    bytes_written=bytes_written,
                                    limit_mb=self.MAX_PDF_BYTES // 1024 // 1024,
                                )
                                break
                            f.write(chunk)
            except Exception as e:
                logger.error(
                    "Failed to download PDF",
                    url=url,
                    error=str(e),
                )
                return None

            # Process the downloaded file
            doc = await self.process_file(file_path)

            # Add URL to metadata
            if doc:
                doc.metadata.url = url

            # Clean up temp file if we created one
            if not save_path and os.path.exists(file_path):
                os.remove(file_path)

            return doc


class PDFSourceFactory:
    """Factory for creating PDF source instances."""

    @staticmethod
    def create(settings: Settings | None = None) -> PDFSource:
        """Create a PDF source with the specified settings."""
        return PDFSource(settings)
