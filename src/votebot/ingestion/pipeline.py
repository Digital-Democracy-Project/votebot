"""Main ingestion pipeline orchestrator."""

import hashlib
from dataclasses import dataclass
from typing import Any, AsyncIterator

import structlog

from votebot.config import Settings, get_settings
from votebot.ingestion.chunking import ChunkingService
from votebot.ingestion.metadata import DocumentMetadata, MetadataExtractor
from votebot.services.vector_store import Document, VectorStoreService

logger = structlog.get_logger()


@dataclass
class IngestionResult:
    """Result of an ingestion operation."""

    documents_processed: int
    chunks_created: int
    chunks_upserted: int
    errors: list[str]
    skipped: int = 0


@dataclass
class DocumentSource:
    """A document to be ingested."""

    content: str
    metadata: DocumentMetadata
    content_hash: str | None = None


class IngestionPipeline:
    """
    Main pipeline for ingesting documents into the vector store.

    Handles:
    - Document processing and chunking
    - Metadata extraction and tagging
    - Duplicate detection
    - Batch upserts to vector store
    """

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the ingestion pipeline.

        Args:
            settings: Application settings. Uses default if not provided.
        """
        self.settings = settings or get_settings()
        self.chunking = ChunkingService(
            chunk_size=self.settings.chunk_size,
            chunk_overlap=self.settings.chunk_overlap,
        )
        self.metadata_extractor = MetadataExtractor()
        self.vector_store = VectorStoreService(self.settings)
        self._processed_hashes: set[str] = set()

    async def ingest_document(
        self,
        content: str,
        metadata: DocumentMetadata,
        skip_duplicates: bool = True,
    ) -> IngestionResult:
        """
        Ingest a single document.

        Args:
            content: Document content
            metadata: Document metadata
            skip_duplicates: Skip if content hash matches existing

        Returns:
            IngestionResult with processing stats
        """
        errors = []
        skipped = 0

        # Calculate content hash
        content_hash = self._hash_content(content)

        # Check for duplicates
        if skip_duplicates and content_hash in self._processed_hashes:
            logger.debug(
                "Skipping duplicate document",
                document_id=metadata.document_id,
            )
            return IngestionResult(
                documents_processed=0,
                chunks_created=0,
                chunks_upserted=0,
                errors=[],
                skipped=1,
            )

        self._processed_hashes.add(content_hash)

        # Chunk the content
        try:
            chunks = self.chunking.chunk_text(content, metadata.to_dict())
        except Exception as e:
            logger.error(
                "Failed to chunk document",
                document_id=metadata.document_id,
                error=str(e),
            )
            return IngestionResult(
                documents_processed=0,
                chunks_created=0,
                chunks_upserted=0,
                errors=[f"Chunking failed: {str(e)}"],
            )

        if not chunks:
            logger.warning(
                "No chunks created for document",
                document_id=metadata.document_id,
            )
            return IngestionResult(
                documents_processed=1,
                chunks_created=0,
                chunks_upserted=0,
                errors=[],
            )

        # Create vector store documents
        documents = []
        for chunk in chunks:
            chunk_id = f"{metadata.document_id}-chunk-{chunk.index}"
            chunk_metadata = {
                **metadata.to_dict(),
                "chunk_index": chunk.index,
                "content_hash": content_hash,
            }

            documents.append(
                Document(
                    id=chunk_id,
                    content=chunk.content,
                    metadata=chunk_metadata,
                )
            )

        # Upsert to vector store
        try:
            upserted = await self.vector_store.upsert_documents(documents)
        except Exception as e:
            logger.error(
                "Failed to upsert documents",
                document_id=metadata.document_id,
                error=str(e),
            )
            return IngestionResult(
                documents_processed=1,
                chunks_created=len(chunks),
                chunks_upserted=0,
                errors=[f"Upsert failed: {str(e)}"],
            )

        logger.info(
            "Document ingested",
            document_id=metadata.document_id,
            chunks=len(chunks),
        )

        return IngestionResult(
            documents_processed=1,
            chunks_created=len(chunks),
            chunks_upserted=upserted,
            errors=errors,
            skipped=skipped,
        )

    async def ingest_batch(
        self,
        sources: list[DocumentSource],
        batch_size: int = 10,
    ) -> IngestionResult:
        """
        Ingest a batch of documents.

        Args:
            sources: List of documents to ingest
            batch_size: Number of documents to process together

        Returns:
            Aggregated IngestionResult
        """
        total_docs = 0
        total_chunks_created = 0
        total_chunks_upserted = 0
        all_errors = []
        total_skipped = 0

        for i in range(0, len(sources), batch_size):
            batch = sources[i : i + batch_size]

            logger.info(
                "Processing batch",
                batch_index=i // batch_size,
                batch_size=len(batch),
            )

            for source in batch:
                result = await self.ingest_document(
                    content=source.content,
                    metadata=source.metadata,
                )

                total_docs += result.documents_processed
                total_chunks_created += result.chunks_created
                total_chunks_upserted += result.chunks_upserted
                all_errors.extend(result.errors)
                total_skipped += result.skipped

        return IngestionResult(
            documents_processed=total_docs,
            chunks_created=total_chunks_created,
            chunks_upserted=total_chunks_upserted,
            errors=all_errors,
            skipped=total_skipped,
        )

    async def ingest_from_source(
        self,
        source_name: str,
        source_config: dict[str, Any],
    ) -> IngestionResult:
        """
        Ingest documents from a configured source.

        Args:
            source_name: Name of the source (congress, openstates, webflow, pdf, website, training_docs)
            source_config: Configuration for the source

        Returns:
            IngestionResult with processing stats
        """
        # Handle special sources that don't follow the standard pattern
        if source_name == "website":
            return await self._ingest_website_pages(source_config)
        elif source_name == "training_docs":
            return await self._ingest_training_docs(source_config)

        from votebot.ingestion.sources import (
            CongressAPISource,
            OpenStatesSource,
            PDFSource,
            WebflowSource,
        )

        source_map = {
            "congress": CongressAPISource,
            "openstates": OpenStatesSource,
            "webflow": WebflowSource,
            "pdf": PDFSource,
        }

        if source_name not in source_map:
            return IngestionResult(
                documents_processed=0,
                chunks_created=0,
                chunks_upserted=0,
                errors=[f"Unknown source: {source_name}"],
            )

        source_class = source_map[source_name]
        source = source_class(self.settings, self.metadata_extractor)

        # Fetch and ingest documents
        documents = []
        async for doc_source in source.fetch(**source_config):
            documents.append(doc_source)

        return await self.ingest_batch(documents)

    async def _ingest_website_pages(self, config: dict[str, Any]) -> IngestionResult:
        """Ingest website pages from a list of URLs."""
        from votebot.ingestion.sources.webflow import WebflowSource

        urls = config.get("urls", [])
        if not urls:
            return IngestionResult(
                documents_processed=0,
                chunks_created=0,
                chunks_upserted=0,
                errors=["No URLs provided"],
            )

        webflow = WebflowSource(self.settings, self.metadata_extractor)
        documents = []
        errors = []

        for url in urls:
            try:
                doc = await webflow.fetch_page(url)
                if doc:
                    documents.append(doc)
                    logger.info(f"Fetched: {url}")
                else:
                    errors.append(f"No content from {url}")
            except Exception as e:
                errors.append(f"Failed to fetch {url}: {str(e)}")
                logger.warning(f"Failed to fetch {url}: {e}")

        result = await self.ingest_batch(documents)
        result.errors.extend(errors)
        return result

    async def _ingest_training_docs(self, config: dict[str, Any]) -> IngestionResult:
        """Ingest training documents from text files."""
        from pathlib import Path

        files = config.get("files", [])
        if not files:
            return IngestionResult(
                documents_processed=0,
                chunks_created=0,
                chunks_upserted=0,
                errors=["No files provided"],
            )

        documents = []
        errors = []

        for file_path in files:
            path = Path(file_path)
            if not path.exists():
                errors.append(f"File not found: {file_path}")
                continue

            try:
                # Try multiple encodings
                content = None
                for encoding in ["utf-8", "latin-1", "cp1252"]:
                    try:
                        content = path.read_text(encoding=encoding)
                        break
                    except UnicodeDecodeError:
                        continue

                if content is None:
                    errors.append(f"Could not decode: {file_path}")
                    continue

                # Create metadata with DDP URL for citation linking
                metadata = DocumentMetadata(
                    document_id=f"training-{path.stem}",
                    document_type="training",
                    source="Digital Democracy Project",
                    title=path.stem.replace("-", " ").replace("_", " ").title(),
                    url="https://digitaldemocracyproject.org",
                    extra={
                        "filename": path.name,
                    },
                )

                documents.append(DocumentSource(content=content, metadata=metadata))
                logger.info(f"Loaded: {path.name}")

            except Exception as e:
                errors.append(f"Failed to read {file_path}: {str(e)}")
                logger.warning(f"Failed to read {file_path}: {e}")

        result = await self.ingest_batch(documents)
        result.errors.extend(errors)
        return result

    async def delete_document(self, document_id: str) -> bool:
        """
        Delete a document and its chunks from the vector store.

        Args:
            document_id: ID of the document to delete

        Returns:
            True if successful
        """
        try:
            # Delete all chunks for this document
            await self.vector_store.delete(
                filter={"document_id": document_id}
            )
            logger.info("Document deleted", document_id=document_id)
            return True
        except Exception as e:
            logger.error(
                "Failed to delete document",
                document_id=document_id,
                error=str(e),
            )
            return False

    def _hash_content(self, content: str) -> str:
        """Generate a hash of the content for duplicate detection."""
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def reset_hash_cache(self) -> None:
        """Reset the processed hashes cache."""
        self._processed_hashes.clear()
