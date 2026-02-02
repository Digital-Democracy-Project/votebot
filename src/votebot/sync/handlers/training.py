"""Training document content handler for unified sync service."""

import time
from pathlib import Path

import structlog

from votebot.config import Settings, get_settings
from votebot.ingestion.metadata import DocumentMetadata
from votebot.ingestion.pipeline import DocumentSource, IngestionPipeline
from votebot.sync.types import ContentType, SyncIdentifier, SyncMode, SyncOptions, SyncResult

logger = structlog.get_logger()


class TrainingHandler:
    """
    Handler for syncing training documents.

    Uses IngestionPipeline to ingest training documents from file paths.
    """

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the training handler.

        Args:
            settings: Application settings
        """
        self.settings = settings or get_settings()
        self._pipeline: IngestionPipeline | None = None

    @property
    def content_type(self) -> ContentType:
        """Return the content type this handler manages."""
        return ContentType.TRAINING

    @property
    def pipeline(self) -> IngestionPipeline:
        """Lazy-initialize IngestionPipeline."""
        if self._pipeline is None:
            self._pipeline = IngestionPipeline(self.settings)
        return self._pipeline

    async def sync_single(
        self,
        identifier: SyncIdentifier,
        options: SyncOptions,
    ) -> SyncResult:
        """
        Sync a single training document.

        Args:
            identifier: Training document identifier (file_path required)
            options: Sync options

        Returns:
            SyncResult with operation status
        """
        start_time = time.perf_counter()
        errors: list[str] = []
        chunks_created = 0
        document_ids: list[str] = []

        if not identifier.file_path:
            return SyncResult(
                success=False,
                content_type=ContentType.TRAINING,
                mode=SyncMode.SINGLE,
                errors=["File path is required for training document sync"],
                duration_seconds=time.perf_counter() - start_time,
            )

        path = Path(identifier.file_path)
        logger.info(
            "Syncing training document",
            file_path=str(path),
        )

        try:
            if not path.exists():
                return SyncResult(
                    success=False,
                    content_type=ContentType.TRAINING,
                    mode=SyncMode.SINGLE,
                    items_processed=1,
                    items_failed=1,
                    errors=[f"File not found: {identifier.file_path}"],
                    duration_seconds=time.perf_counter() - start_time,
                )

            if options.dry_run:
                return SyncResult(
                    success=True,
                    content_type=ContentType.TRAINING,
                    mode=SyncMode.SINGLE,
                    items_processed=1,
                    items_successful=1,
                    duration_seconds=time.perf_counter() - start_time,
                )

            # Read file content with multiple encoding attempts
            content = None
            for encoding in ["utf-8", "latin-1", "cp1252"]:
                try:
                    content = path.read_text(encoding=encoding)
                    break
                except UnicodeDecodeError:
                    continue

            if content is None:
                return SyncResult(
                    success=False,
                    content_type=ContentType.TRAINING,
                    mode=SyncMode.SINGLE,
                    items_processed=1,
                    items_failed=1,
                    errors=[f"Could not decode file: {identifier.file_path}"],
                    duration_seconds=time.perf_counter() - start_time,
                )

            # Create metadata
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

            # Ingest to vector store
            result = await self.pipeline.ingest_document(
                content=content,
                metadata=metadata,
                skip_duplicates=False,
            )

            chunks_created = result.chunks_created
            document_ids.append(metadata.document_id)

            success = result.chunks_created > 0
            duration = time.perf_counter() - start_time

            logger.info(
                "Training document sync complete",
                file_path=str(path),
                success=success,
                chunks_created=chunks_created,
                duration_seconds=round(duration, 2),
            )

            return SyncResult(
                success=success,
                content_type=ContentType.TRAINING,
                mode=SyncMode.SINGLE,
                items_processed=1,
                items_successful=1 if success else 0,
                items_failed=0 if success else 1,
                chunks_created=chunks_created,
                duration_seconds=duration,
                errors=errors,
                document_ids=document_ids,
            )

        except Exception as e:
            logger.exception("Training document sync failed", error=str(e))
            return SyncResult(
                success=False,
                content_type=ContentType.TRAINING,
                mode=SyncMode.SINGLE,
                items_processed=1,
                items_failed=1,
                errors=[str(e)],
                duration_seconds=time.perf_counter() - start_time,
            )

    async def sync_batch(
        self,
        options: SyncOptions,
    ) -> SyncResult:
        """
        Batch sync training documents from a directory.

        Note: Batch mode requires a directory path to be set via identifier
        in the sync_single method. For true batch operations, use the
        IngestionPipeline directly with ingest_from_source().

        Args:
            options: Sync options (ignored)

        Returns:
            SyncResult indicating batch mode usage
        """
        return SyncResult(
            success=False,
            content_type=ContentType.TRAINING,
            mode=SyncMode.BATCH,
            errors=[
                "Batch mode for training documents requires explicit file paths. "
                "Use single mode with --path for each document, or use the "
                "IngestionPipeline.ingest_from_source() method directly."
            ],
            duration_seconds=0.0,
        )

    async def sync_directory(
        self,
        directory: str | Path,
        options: SyncOptions,
        pattern: str = "*.txt",
    ) -> SyncResult:
        """
        Sync all training documents from a directory.

        Args:
            directory: Path to directory containing training documents
            options: Sync options
            pattern: Glob pattern for files to include (default: *.txt)

        Returns:
            SyncResult with aggregated stats
        """
        start_time = time.perf_counter()
        errors: list[str] = []
        total_processed = 0
        total_successful = 0
        total_chunks = 0
        document_ids: list[str] = []

        dir_path = Path(directory)
        if not dir_path.exists() or not dir_path.is_dir():
            return SyncResult(
                success=False,
                content_type=ContentType.TRAINING,
                mode=SyncMode.BATCH,
                errors=[f"Directory not found: {directory}"],
                duration_seconds=time.perf_counter() - start_time,
            )

        logger.info(
            "Starting training document batch sync",
            directory=str(dir_path),
            pattern=pattern,
        )

        # Find all matching files
        files = list(dir_path.glob(pattern))

        if options.limit > 0:
            files = files[: options.limit]

        logger.info(f"Found {len(files)} training documents")

        if options.dry_run:
            return SyncResult(
                success=True,
                content_type=ContentType.TRAINING,
                mode=SyncMode.BATCH,
                items_processed=len(files),
                items_successful=len(files),
                duration_seconds=time.perf_counter() - start_time,
            )

        # Process each file
        documents = []
        for file_path in files:
            try:
                # Read file content
                content = None
                for encoding in ["utf-8", "latin-1", "cp1252"]:
                    try:
                        content = file_path.read_text(encoding=encoding)
                        break
                    except UnicodeDecodeError:
                        continue

                if content is None:
                    errors.append(f"Could not decode: {file_path}")
                    continue

                # Create metadata
                metadata = DocumentMetadata(
                    document_id=f"training-{file_path.stem}",
                    document_type="training",
                    source="Digital Democracy Project",
                    title=file_path.stem.replace("-", " ").replace("_", " ").title(),
                    url="https://digitaldemocracyproject.org",
                    extra={
                        "filename": file_path.name,
                    },
                )

                documents.append(DocumentSource(content=content, metadata=metadata))
                logger.debug(f"Loaded: {file_path.name}")

            except Exception as e:
                errors.append(f"Failed to read {file_path}: {str(e)}")

        total_processed = len(files)

        # Ingest batch
        if documents:
            result = await self.pipeline.ingest_batch(documents)
            total_successful = result.documents_processed
            total_chunks = result.chunks_created
            errors.extend(result.errors)

        duration = time.perf_counter() - start_time
        success = total_successful > 0

        logger.info(
            "Training document batch sync complete",
            processed=total_processed,
            successful=total_successful,
            chunks_created=total_chunks,
            duration_seconds=round(duration, 2),
        )

        return SyncResult(
            success=success,
            content_type=ContentType.TRAINING,
            mode=SyncMode.BATCH,
            items_processed=total_processed,
            items_successful=total_successful,
            items_failed=total_processed - total_successful,
            chunks_created=total_chunks,
            duration_seconds=duration,
            errors=errors,
            document_ids=document_ids,
        )
