"""Unified sync service for all content types."""

import time

import structlog

from votebot.config import Settings, get_settings
from votebot.sync.handlers.bill import BillHandler
from votebot.sync.handlers.legislator import LegislatorHandler
from votebot.sync.handlers.organization import OrganizationHandler
from votebot.sync.handlers.training import TrainingHandler
from votebot.sync.handlers.webpage import WebpageHandler
from votebot.sync.types import ContentType, SyncIdentifier, SyncMode, SyncOptions, SyncResult

logger = structlog.get_logger()


class UnifiedSyncService:
    """
    Unified service for syncing all content types to the vector store.

    Provides a single entry point for syncing bills, legislators, organizations,
    webpages, and training documents with consistent options and result handling.
    """

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the unified sync service.

        Args:
            settings: Application settings
        """
        self.settings = settings or get_settings()
        self._handlers: dict[ContentType, BillHandler | LegislatorHandler | OrganizationHandler | WebpageHandler | TrainingHandler] = {}

    def _get_handler(
        self, content_type: ContentType
    ) -> BillHandler | LegislatorHandler | OrganizationHandler | WebpageHandler | TrainingHandler:
        """
        Get or create a handler for the specified content type.

        Args:
            content_type: The content type to get a handler for

        Returns:
            Handler instance for the content type
        """
        if content_type not in self._handlers:
            handler_map = {
                ContentType.BILL: BillHandler,
                ContentType.LEGISLATOR: LegislatorHandler,
                ContentType.ORGANIZATION: OrganizationHandler,
                ContentType.WEBPAGE: WebpageHandler,
                ContentType.TRAINING: TrainingHandler,
            }
            handler_class = handler_map[content_type]
            self._handlers[content_type] = handler_class(self.settings)

        return self._handlers[content_type]

    async def sync(
        self,
        content_type: ContentType,
        mode: SyncMode,
        identifier: SyncIdentifier | None = None,
        options: SyncOptions | None = None,
    ) -> SyncResult:
        """
        Sync content of the specified type.

        Args:
            content_type: Type of content to sync
            mode: Single item or batch sync
            identifier: Identifier for single item sync (required for SINGLE mode)
            options: Sync options

        Returns:
            SyncResult with operation status and stats
        """
        options = options or SyncOptions()
        handler = self._get_handler(content_type)

        logger.info(
            "Starting sync",
            content_type=content_type.value,
            mode=mode.value,
            identifier=identifier.primary_identifier if identifier else None,
            dry_run=options.dry_run,
        )

        if mode == SyncMode.SINGLE:
            if identifier is None:
                return SyncResult(
                    success=False,
                    content_type=content_type,
                    mode=mode,
                    errors=["Identifier is required for single item sync"],
                )
            return await handler.sync_single(identifier, options)
        else:
            return await handler.sync_batch(options)

    async def sync_all(
        self,
        options: SyncOptions | None = None,
        content_types: list[ContentType] | None = None,
    ) -> dict[ContentType, SyncResult]:
        """
        Sync all content types in batch mode.

        Args:
            options: Sync options
            content_types: Specific content types to sync (default: all except webpage/training)

        Returns:
            Dict mapping content types to their sync results
        """
        options = options or SyncOptions()
        start_time = time.perf_counter()

        # Default to the main content types that support batch
        if content_types is None:
            content_types = [
                ContentType.BILL,
                ContentType.LEGISLATOR,
                ContentType.ORGANIZATION,
            ]

        logger.info(
            "Starting sync_all",
            content_types=[ct.value for ct in content_types],
            dry_run=options.dry_run,
        )

        results: dict[ContentType, SyncResult] = {}

        for content_type in content_types:
            logger.info(f"Syncing {content_type.value}...")
            result = await self.sync(
                content_type=content_type,
                mode=SyncMode.BATCH,
                options=options,
            )
            results[content_type] = result

            logger.info(
                f"{content_type.value} sync complete",
                success=result.success,
                items_processed=result.items_processed,
                chunks_created=result.chunks_created,
            )

        total_duration = time.perf_counter() - start_time
        total_items = sum(r.items_processed for r in results.values())
        total_chunks = sum(r.chunks_created for r in results.values())
        total_successful = sum(r.items_successful for r in results.values())

        logger.info(
            "sync_all complete",
            total_items=total_items,
            total_successful=total_successful,
            total_chunks=total_chunks,
            duration_seconds=round(total_duration, 2),
        )

        return results

    def get_supported_identifiers(self, content_type: ContentType) -> list[str]:
        """
        Get the supported identifier types for a content type.

        Args:
            content_type: The content type to check

        Returns:
            List of supported identifier field names
        """
        identifier_map = {
            ContentType.BILL: ["webflow_id", "slug", "openstates_id"],
            ContentType.LEGISLATOR: ["webflow_id", "slug", "openstates_id"],
            ContentType.ORGANIZATION: ["webflow_id", "slug"],
            ContentType.WEBPAGE: ["url"],
            ContentType.TRAINING: ["file_path"],
        }
        return identifier_map.get(content_type, [])
