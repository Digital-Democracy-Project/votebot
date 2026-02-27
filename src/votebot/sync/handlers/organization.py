"""Organization content handler for unified sync service."""

import time

import structlog

from votebot.config import Settings, get_settings
from votebot.ingestion.pipeline import IngestionPipeline
from votebot.ingestion.sources.webflow import WebflowSource
from votebot.sync.types import ContentType, SyncIdentifier, SyncMode, SyncOptions, SyncResult
from votebot.updates.organization_sync import OrganizationSyncService

logger = structlog.get_logger()


class OrganizationHandler:
    """
    Handler for syncing organization content.

    Wraps existing OrganizationSyncService and WebflowSource to provide
    both single-item and batch sync capabilities.
    """

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the organization handler.

        Args:
            settings: Application settings
        """
        self.settings = settings or get_settings()
        self._webflow: WebflowSource | None = None
        self._org_sync: OrganizationSyncService | None = None
        self._pipeline: IngestionPipeline | None = None

    @property
    def content_type(self) -> ContentType:
        """Return the content type this handler manages."""
        return ContentType.ORGANIZATION

    @property
    def webflow(self) -> WebflowSource:
        """Lazy-initialize WebflowSource."""
        if self._webflow is None:
            self._webflow = WebflowSource(self.settings)
        return self._webflow

    @property
    def org_sync(self) -> OrganizationSyncService:
        """Lazy-initialize OrganizationSyncService."""
        if self._org_sync is None:
            self._org_sync = OrganizationSyncService(self.settings)
        return self._org_sync

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
        Sync a single organization.

        Args:
            identifier: Organization identifier (webflow_id or slug)
            options: Sync options

        Returns:
            SyncResult with operation status
        """
        start_time = time.perf_counter()
        errors: list[str] = []
        chunks_created = 0
        document_ids: list[str] = []

        logger.info(
            "Syncing single organization",
            identifier=identifier.primary_identifier,
        )

        try:
            collection_id = self.settings.webflow_organizations_collection_id
            if not collection_id:
                return SyncResult(
                    success=False,
                    content_type=ContentType.ORGANIZATION,
                    mode=SyncMode.SINGLE,
                    errors=["Organizations collection ID not configured"],
                    duration_seconds=time.perf_counter() - start_time,
                )

            # Fetch the item from Webflow
            item = None
            if identifier.webflow_id:
                item = await self.webflow.fetch_item_by_id(
                    collection_id, identifier.webflow_id
                )
            elif identifier.slug:
                item = await self.webflow.fetch_item_by_slug(
                    collection_id, identifier.slug
                )

            if not item:
                return SyncResult(
                    success=False,
                    content_type=ContentType.ORGANIZATION,
                    mode=SyncMode.SINGLE,
                    errors=[f"Organization not found: {identifier.primary_identifier}"],
                    duration_seconds=time.perf_counter() - start_time,
                )

            fields = item.get("fieldData", {})
            item_id = item.get("id", "")
            name = fields.get("name", "Unknown Organization")

            logger.info(
                "Organization found",
                item_id=item_id,
                name=name,
            )

            if options.dry_run:
                return SyncResult(
                    success=True,
                    content_type=ContentType.ORGANIZATION,
                    mode=SyncMode.SINGLE,
                    items_processed=1,
                    items_successful=1,
                    duration_seconds=time.perf_counter() - start_time,
                )

            # Use OrganizationSyncService for content processing
            result = await self.org_sync.sync_organization(item)

            if result.success:
                chunks_created = result.chunks_created
                document_ids.append(f"organization-{item_id}")
            else:
                errors.append(result.error or "Unknown error")

            success = result.success
            duration = time.perf_counter() - start_time

            logger.info(
                "Organization sync complete",
                item_id=item_id,
                success=success,
                chunks_created=chunks_created,
                duration_seconds=round(duration, 2),
            )

            return SyncResult(
                success=success,
                content_type=ContentType.ORGANIZATION,
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
            logger.exception("Organization sync failed", error=str(e))
            return SyncResult(
                success=False,
                content_type=ContentType.ORGANIZATION,
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
        Sync all organizations from Webflow.

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

        logger.info(
            "Starting organization batch sync",
            limit=options.limit if options.limit > 0 else "unlimited",
        )

        try:
            # Fetch all organizations from Webflow
            organizations = []
            async for doc in self.webflow.fetch_organizations(limit=options.limit):
                organizations.append(doc)

            logger.info(f"Fetched {len(organizations)} organizations from Webflow")

            if options.dry_run:
                return SyncResult(
                    success=True,
                    content_type=ContentType.ORGANIZATION,
                    mode=SyncMode.BATCH,
                    items_processed=len(organizations),
                    items_successful=len(organizations),
                    duration_seconds=time.perf_counter() - start_time,
                )

            # Ingest to vector store
            result = await self.pipeline.ingest_batch(organizations)

            total_processed = len(organizations)
            total_successful = result.documents_processed
            total_chunks = result.chunks_created
            errors.extend(result.errors)

            duration = time.perf_counter() - start_time
            success = total_successful > 0

            # Report final progress
            if options.progress_callback:
                await options.progress_callback(
                    items_processed=total_processed,
                    items_successful=total_successful,
                    items_failed=total_processed - total_successful,
                    chunks_created=total_chunks,
                    errors=errors,
                )

            logger.info(
                "Organization batch sync complete",
                processed=total_processed,
                successful=total_successful,
                chunks_created=total_chunks,
                duration_seconds=round(duration, 2),
            )

            return SyncResult(
                success=success,
                content_type=ContentType.ORGANIZATION,
                mode=SyncMode.BATCH,
                items_processed=total_processed,
                items_successful=total_successful,
                items_failed=total_processed - total_successful,
                chunks_created=total_chunks,
                duration_seconds=duration,
                errors=errors,
                document_ids=document_ids,
            )

        except Exception as e:
            logger.exception("Organization batch sync failed", error=str(e))
            return SyncResult(
                success=False,
                content_type=ContentType.ORGANIZATION,
                mode=SyncMode.BATCH,
                items_processed=total_processed,
                items_successful=total_successful,
                items_failed=total_processed - total_successful,
                chunks_created=total_chunks,
                errors=[str(e)] + errors,
                duration_seconds=time.perf_counter() - start_time,
            )
