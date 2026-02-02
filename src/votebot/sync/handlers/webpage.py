"""Webpage content handler for unified sync service."""

import time

import structlog

from votebot.config import Settings, get_settings
from votebot.ingestion.pipeline import IngestionPipeline
from votebot.ingestion.sources.webflow import WebflowSource
from votebot.sync.types import ContentType, SyncIdentifier, SyncMode, SyncOptions, SyncResult

logger = structlog.get_logger()


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
        Batch sync is not supported for webpages.

        Webpages must be synced individually by URL.

        Args:
            options: Sync options (ignored)

        Returns:
            SyncResult indicating batch mode is not supported
        """
        return SyncResult(
            success=False,
            content_type=ContentType.WEBPAGE,
            mode=SyncMode.BATCH,
            errors=["Batch mode is not supported for webpages. Use single mode with a URL."],
            duration_seconds=0.0,
        )
