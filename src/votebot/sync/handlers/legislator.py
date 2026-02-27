"""Legislator content handler for unified sync service."""

import time

import httpx
import structlog

from votebot.config import Settings, get_settings
from votebot.ingestion.pipeline import IngestionPipeline
from votebot.ingestion.sources.webflow import WebflowSource
from votebot.sync.types import ContentType, SyncIdentifier, SyncMode, SyncOptions, SyncResult
from votebot.updates.legislator_sync import LegislatorSyncService

logger = structlog.get_logger()


class LegislatorHandler:
    """
    Handler for syncing legislator content.

    Wraps existing LegislatorSyncService and WebflowSource to provide
    both single-item and batch sync capabilities.
    """

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the legislator handler.

        Args:
            settings: Application settings
        """
        self.settings = settings or get_settings()
        self._webflow: WebflowSource | None = None
        self._legislator_sync: LegislatorSyncService | None = None
        self._pipeline: IngestionPipeline | None = None

    @property
    def content_type(self) -> ContentType:
        """Return the content type this handler manages."""
        return ContentType.LEGISLATOR

    @property
    def webflow(self) -> WebflowSource:
        """Lazy-initialize WebflowSource."""
        if self._webflow is None:
            self._webflow = WebflowSource(self.settings)
        return self._webflow

    @property
    def legislator_sync(self) -> LegislatorSyncService:
        """Lazy-initialize LegislatorSyncService."""
        if self._legislator_sync is None:
            self._legislator_sync = LegislatorSyncService(self.settings)
        return self._legislator_sync

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
        Sync a single legislator.

        Args:
            identifier: Legislator identifier (webflow_id or slug)
            options: Sync options

        Returns:
            SyncResult with operation status
        """
        start_time = time.perf_counter()
        errors: list[str] = []
        chunks_created = 0
        document_ids: list[str] = []

        logger.info(
            "Syncing single legislator",
            identifier=identifier.primary_identifier,
            include_sponsored_bills=options.include_sponsored_bills,
        )

        try:
            collection_id = self.settings.webflow_legislators_collection_id
            if not collection_id:
                return SyncResult(
                    success=False,
                    content_type=ContentType.LEGISLATOR,
                    mode=SyncMode.SINGLE,
                    errors=["Legislators collection ID not configured"],
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
                    content_type=ContentType.LEGISLATOR,
                    mode=SyncMode.SINGLE,
                    errors=[f"Legislator not found: {identifier.primary_identifier}"],
                    duration_seconds=time.perf_counter() - start_time,
                )

            fields = item.get("fieldData", {})
            item_id = item.get("id", "")
            name = fields.get("name", "Unknown Legislator")
            openstates_id = fields.get("openstatesid", "")
            slug = fields.get("slug", "")
            party = fields.get("party-2", fields.get("party", ""))
            chamber = fields.get("chamber", "")

            # Store jurisdiction ref for resolution after building mapping
            jurisdiction_ref = fields.get("jurisdiction")

            logger.info(
                "Legislator found",
                item_id=item_id,
                name=name,
                has_openstates_id=bool(openstates_id),
            )

            if options.dry_run:
                return SyncResult(
                    success=True,
                    content_type=ContentType.LEGISLATOR,
                    mode=SyncMode.SINGLE,
                    items_processed=1,
                    items_successful=1,
                    duration_seconds=time.perf_counter() - start_time,
                )

            # Build jurisdiction mapping for Webflow content processing
            async with httpx.AsyncClient(timeout=60.0) as client:
                headers = {
                    "Authorization": f"Bearer {self.settings.webflow_votebot_api_key.get_secret_value()}",
                    "accept": "application/json",
                }
                await self.webflow._build_jurisdiction_mapping(client, headers)

            # Resolve jurisdiction using the mapping
            jurisdiction = self.webflow._resolve_jurisdiction(jurisdiction_ref).lower()

            # Process the legislator item for Webflow content
            doc = self.webflow._process_legislator_item(item)
            if doc:
                cms_result = await self.pipeline.ingest_document(
                    content=doc.content,
                    metadata=doc.metadata,
                    skip_duplicates=False,
                )
                chunks_created += cms_result.chunks_created
                document_ids.append(doc.metadata.document_id)

            # Sync sponsored bills from OpenStates if enabled
            # Note: Vote syncing is now handled during bill sync (bill-votes documents)
            if openstates_id and options.include_sponsored_bills:
                legislator_data = {
                    "openstates_id": openstates_id,
                    "name": name,
                    "slug": slug,
                    "jurisdiction": jurisdiction,
                    "party": party,
                    "chamber": chamber,
                }

                result = await self.legislator_sync.sync_legislator(
                    legislator_data,
                    include_votes=options.include_votes,
                    vote_session=options.vote_session,
                    max_vote_bills=options.max_vote_bills,
                )

                if result.success:
                    chunks_created += result.chunks_created
                    document_ids.append(f"legislator-bills-{openstates_id}")
                else:
                    errors.append(f"Legislator sync failed: {result.error}")

            success = len(errors) == 0 or chunks_created > 0
            duration = time.perf_counter() - start_time

            logger.info(
                "Legislator sync complete",
                item_id=item_id,
                success=success,
                chunks_created=chunks_created,
                duration_seconds=round(duration, 2),
            )

            return SyncResult(
                success=success,
                content_type=ContentType.LEGISLATOR,
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
            logger.exception("Legislator sync failed", error=str(e))
            return SyncResult(
                success=False,
                content_type=ContentType.LEGISLATOR,
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
        Sync all legislators from Webflow.

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
            "Starting legislator batch sync",
            limit=options.limit if options.limit > 0 else "unlimited",
            include_sponsored_bills=options.include_sponsored_bills,
            jurisdiction=options.jurisdiction,
        )

        try:
            # Fetch all legislators from Webflow
            legislators = []
            async for doc in self.webflow.fetch_legislators(limit=options.limit):
                # Filter by jurisdiction if specified
                if options.jurisdiction:
                    if doc.metadata.jurisdiction != options.jurisdiction.upper():
                        continue
                legislators.append(doc)

            logger.info(f"Fetched {len(legislators)} legislators from Webflow")

            if options.dry_run:
                return SyncResult(
                    success=True,
                    content_type=ContentType.LEGISLATOR,
                    mode=SyncMode.BATCH,
                    items_processed=len(legislators),
                    items_successful=len(legislators),
                    duration_seconds=time.perf_counter() - start_time,
                )

            # Ingest Webflow content to vector store
            result = await self.pipeline.ingest_batch(legislators)

            total_processed = len(legislators)
            total_successful = result.documents_processed
            total_chunks = result.chunks_created
            errors.extend(result.errors)

            # Also sync sponsored bills if enabled
            # Note: Vote syncing is now handled during bill sync (bill-votes documents)
            if options.include_sponsored_bills:
                # Build legislator data for OpenStates sync
                legislator_data_list = []
                for doc in legislators:
                    if doc.metadata.legislator_id:
                        legislator_data_list.append({
                            "openstates_id": doc.metadata.legislator_id,
                            "name": doc.metadata.title,
                            "slug": doc.metadata.extra.get("slug", ""),
                            "jurisdiction": doc.metadata.jurisdiction or "us",
                            "party": doc.metadata.extra.get("party", ""),
                            "chamber": doc.metadata.extra.get("chamber", ""),
                        })

                if legislator_data_list:
                    os_result = await self.legislator_sync.sync_all_legislators(
                        legislator_data_list,
                        include_votes=options.include_votes,
                        vote_session=options.vote_session,
                        max_vote_bills=options.max_vote_bills,
                    )
                    total_chunks += os_result.chunks_created
                    errors.extend(os_result.errors)

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
                "Legislator batch sync complete",
                processed=total_processed,
                successful=total_successful,
                chunks_created=total_chunks,
                duration_seconds=round(duration, 2),
            )

            return SyncResult(
                success=success,
                content_type=ContentType.LEGISLATOR,
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
            logger.exception("Legislator batch sync failed", error=str(e))
            return SyncResult(
                success=False,
                content_type=ContentType.LEGISLATOR,
                mode=SyncMode.BATCH,
                items_processed=total_processed,
                items_successful=total_successful,
                items_failed=total_processed - total_successful,
                chunks_created=total_chunks,
                errors=[str(e)] + errors,
                duration_seconds=time.perf_counter() - start_time,
            )
