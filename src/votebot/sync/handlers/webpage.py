"""Webpage content handler for unified sync service."""

import time
from pathlib import Path

import structlog

from votebot.config import Settings, get_settings
from votebot.ingestion.pipeline import IngestionPipeline
from votebot.ingestion.sources.webflow import WebflowSource
from votebot.sync.types import ContentType, SyncIdentifier, SyncMode, SyncOptions, SyncResult

logger = structlog.get_logger()

# Default path to website pages file (relative to project root)
DEFAULT_WEBSITE_PAGES_FILE = Path(__file__).parent.parent.parent.parent.parent / "RAG training docs" / "website_pages.txt"


class WebpageHandler:
    """
    Handler for syncing webpage content.

    Uses WebflowSource.fetch_page() to scrape and ingest webpage content.
    """

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the webpage handler.

        Args:
            settings: Application settings
        """
        self.settings = settings or get_settings()
        self._webflow: WebflowSource | None = None
        self._pipeline: IngestionPipeline | None = None

    @property
    def content_type(self) -> ContentType:
        """Return the content type this handler manages."""
        return ContentType.WEBPAGE

    @property
    def webflow(self) -> WebflowSource:
        """Lazy-initialize WebflowSource."""
        if self._webflow is None:
            self._webflow = WebflowSource(self.settings)
        return self._webflow

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
        Sync a single webpage.

        Args:
            identifier: Webpage identifier (url required)
            options: Sync options

        Returns:
            SyncResult with operation status
        """
        start_time = time.perf_counter()
        errors: list[str] = []
        chunks_created = 0
        document_ids: list[str] = []

        if not identifier.url:
            return SyncResult(
                success=False,
                content_type=ContentType.WEBPAGE,
                mode=SyncMode.SINGLE,
                errors=["URL is required for webpage sync"],
                duration_seconds=time.perf_counter() - start_time,
            )

        logger.info(
            "Syncing webpage",
            url=identifier.url,
        )

        try:
            if options.dry_run:
                return SyncResult(
                    success=True,
                    content_type=ContentType.WEBPAGE,
                    mode=SyncMode.SINGLE,
                    items_processed=1,
                    items_successful=1,
                    duration_seconds=time.perf_counter() - start_time,
                )

            # Fetch the webpage
            doc = await self.webflow.fetch_page(identifier.url)

            if not doc:
                return SyncResult(
                    success=False,
                    content_type=ContentType.WEBPAGE,
                    mode=SyncMode.SINGLE,
                    items_processed=1,
                    items_failed=1,
                    errors=[f"Failed to fetch webpage: {identifier.url}"],
                    duration_seconds=time.perf_counter() - start_time,
                )

            # Ingest to vector store
            result = await self.pipeline.ingest_document(
                content=doc.content,
                metadata=doc.metadata,
                skip_duplicates=False,
            )

            chunks_created = result.chunks_created
            document_ids.append(doc.metadata.document_id)

            success = result.chunks_created > 0
            duration = time.perf_counter() - start_time

            logger.info(
                "Webpage sync complete",
                url=identifier.url,
                success=success,
                chunks_created=chunks_created,
                duration_seconds=round(duration, 2),
            )

            return SyncResult(
                success=success,
                content_type=ContentType.WEBPAGE,
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
            logger.exception("Webpage sync failed", error=str(e))
            return SyncResult(
                success=False,
                content_type=ContentType.WEBPAGE,
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
        Batch sync all webpages from the website_pages.txt file.

        Reads URLs from the default website_pages.txt file and ingests each one.

        Args:
            options: Sync options

        Returns:
            SyncResult with aggregated stats
        """
        start_time = time.perf_counter()
        errors: list[str] = []
        total_processed = 0
        total_successful = 0
        total_chunks = 0
        document_ids: list[str] = []

        # Find the website pages file
        pages_file = DEFAULT_WEBSITE_PAGES_FILE
        if not pages_file.exists():
            # Try alternate location (deployed server path)
            alt_path = Path("/home/ubuntu/votebot/RAG training docs/website_pages.txt")
            if alt_path.exists():
                pages_file = alt_path
            else:
                return SyncResult(
                    success=False,
                    content_type=ContentType.WEBPAGE,
                    mode=SyncMode.BATCH,
                    errors=[f"Website pages file not found: {pages_file}"],
                    duration_seconds=time.perf_counter() - start_time,
                )

        # Read URLs from file
        urls = []
        try:
            with open(pages_file, "r") as f:
                for line in f:
                    line = line.strip()
                    # Skip empty lines and comments
                    if line and not line.startswith("#"):
                        urls.append(line)
        except Exception as e:
            return SyncResult(
                success=False,
                content_type=ContentType.WEBPAGE,
                mode=SyncMode.BATCH,
                errors=[f"Failed to read website pages file: {e}"],
                duration_seconds=time.perf_counter() - start_time,
            )

        if not urls:
            return SyncResult(
                success=False,
                content_type=ContentType.WEBPAGE,
                mode=SyncMode.BATCH,
                errors=["No URLs found in website pages file"],
                duration_seconds=time.perf_counter() - start_time,
            )

        # Apply limit if specified
        if options.limit > 0:
            urls = urls[:options.limit]

        logger.info(
            "Starting webpage batch sync",
            pages_file=str(pages_file),
            url_count=len(urls),
        )

        if options.dry_run:
            return SyncResult(
                success=True,
                content_type=ContentType.WEBPAGE,
                mode=SyncMode.BATCH,
                items_processed=len(urls),
                items_successful=len(urls),
                duration_seconds=time.perf_counter() - start_time,
            )

        # Process each URL
        for url in urls:
            total_processed += 1
            try:
                doc = await self.webflow.fetch_page(url)
                if not doc:
                    errors.append(f"Failed to fetch: {url}")
                    continue

                result = await self.pipeline.ingest_document(
                    content=doc.content,
                    metadata=doc.metadata,
                    skip_duplicates=False,
                )

                if result.chunks_created > 0:
                    total_successful += 1
                    total_chunks += result.chunks_created
                    document_ids.append(doc.metadata.document_id)
                    logger.debug(f"Synced webpage: {url} ({result.chunks_created} chunks)")
                else:
                    errors.append(f"No chunks created for: {url}")

            except Exception as e:
                errors.append(f"Error processing {url}: {str(e)}")
                logger.warning(f"Failed to sync webpage: {url}", error=str(e))

        duration = time.perf_counter() - start_time
        success = total_successful > 0

        logger.info(
            "Webpage batch sync complete",
            processed=total_processed,
            successful=total_successful,
            chunks_created=total_chunks,
            duration_seconds=round(duration, 2),
        )

        return SyncResult(
            success=success,
            content_type=ContentType.WEBPAGE,
            mode=SyncMode.BATCH,
            items_processed=total_processed,
            items_successful=total_successful,
            items_failed=total_processed - total_successful,
            chunks_created=total_chunks,
            duration_seconds=duration,
            errors=errors,
            document_ids=document_ids,
        )
