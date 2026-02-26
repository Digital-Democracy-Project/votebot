"""Bill content handler for unified sync service."""

import time

import httpx
import structlog

from votebot.config import Settings, get_settings
from votebot.ingestion.pipeline import IngestionPipeline
from votebot.ingestion.sources.webflow import WebflowSource
from votebot.sync.types import ContentType, SyncIdentifier, SyncMode, SyncOptions, SyncResult
from votebot.updates.bill_sync import BillSyncService

logger = structlog.get_logger()


class BillHandler:
    """
    Handler for syncing bill content.

    Wraps existing BillSyncService and WebflowSource to provide
    both single-item and batch sync capabilities.
    """

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the bill handler.

        Args:
            settings: Application settings
        """
        self.settings = settings or get_settings()
        self._webflow: WebflowSource | None = None
        self._bill_sync: BillSyncService | None = None
        self._pipeline: IngestionPipeline | None = None

    @property
    def content_type(self) -> ContentType:
        """Return the content type this handler manages."""
        return ContentType.BILL

    @property
    def webflow(self) -> WebflowSource:
        """Lazy-initialize WebflowSource."""
        if self._webflow is None:
            self._webflow = WebflowSource(self.settings)
        return self._webflow

    @property
    def bill_sync(self) -> BillSyncService:
        """Lazy-initialize BillSyncService."""
        if self._bill_sync is None:
            self._bill_sync = BillSyncService(self.settings)
        return self._bill_sync

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
        Sync a single bill.

        Args:
            identifier: Bill identifier (webflow_id or slug)
            options: Sync options

        Returns:
            SyncResult with operation status
        """
        start_time = time.perf_counter()
        errors: list[str] = []
        chunks_created = 0
        document_ids: list[str] = []

        logger.info(
            "Syncing single bill",
            identifier=identifier.primary_identifier,
            include_pdfs=options.include_pdfs,
            include_openstates=options.include_openstates,
        )

        try:
            collection_id = self.settings.webflow_bills_collection_id
            if not collection_id:
                return SyncResult(
                    success=False,
                    content_type=ContentType.BILL,
                    mode=SyncMode.SINGLE,
                    errors=["Bills collection ID not configured"],
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
                    content_type=ContentType.BILL,
                    mode=SyncMode.SINGLE,
                    errors=[f"Bill not found: {identifier.primary_identifier}"],
                    duration_seconds=time.perf_counter() - start_time,
                )

            fields = item.get("fieldData", {})
            item_id = item.get("id", "")
            title = fields.get("name", "Unknown Bill")
            openstates_url = fields.get("open-states-url-2", "")
            jurisdiction_id = fields.get("jurisdiction", "")
            slug = fields.get("slug", "")

            logger.info(
                "Bill found",
                item_id=item_id,
                title=title,
                has_openstates_url=bool(openstates_url),
            )

            if options.dry_run:
                return SyncResult(
                    success=True,
                    content_type=ContentType.BILL,
                    mode=SyncMode.SINGLE,
                    items_processed=1,
                    items_successful=1,
                    duration_seconds=time.perf_counter() - start_time,
                )

            # Resolve jurisdiction for tracking and OpenStates sync
            jurisdiction_code = self.bill_sync.resolve_jurisdiction_code(
                jurisdiction_id, openstates_url
            )

            # Sync OpenStates history if available and enabled
            if openstates_url and options.include_openstates:
                result = await self.bill_sync.sync_bill(
                    openstates_url=openstates_url,
                    webflow_bill_id=item_id,
                    bill_title=title,
                    jurisdiction_name=jurisdiction_code,
                    bill_slug=slug,
                )

                if result.success:
                    chunks_created += result.chunks_created
                    document_ids.append(f"bill-history-{item_id}")
                else:
                    errors.append(f"OpenStates sync failed: {result.error}")

            # Build organization mapping for bill content
            async with httpx.AsyncClient(timeout=60.0) as client:
                headers = {
                    "Authorization": f"Bearer {self.settings.webflow_votebot_api_key.get_secret_value()}",
                    "accept": "application/json",
                }
                await self.webflow._build_organization_mapping(client, headers)

            # Process the bill item for CMS content and PDF
            async for doc in self.webflow._process_bill_item(
                item, include_pdfs=options.include_pdfs
            ):
                cms_result = await self.pipeline.ingest_document(
                    content=doc.content,
                    metadata=doc.metadata,
                    skip_duplicates=False,
                )
                chunks_created += cms_result.chunks_created
                document_ids.append(doc.metadata.document_id)

            success = len(errors) == 0 or chunks_created > 0
            duration = time.perf_counter() - start_time

            # Track active jurisdiction in Redis
            if jurisdiction_code:
                from votebot.services.redis_store import get_redis_store

                await get_redis_store().add_active_jurisdiction(jurisdiction_code)

            logger.info(
                "Bill sync complete",
                item_id=item_id,
                success=success,
                chunks_created=chunks_created,
                duration_seconds=round(duration, 2),
            )

            return SyncResult(
                success=success,
                content_type=ContentType.BILL,
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
            logger.exception("Bill sync failed", error=str(e))
            return SyncResult(
                success=False,
                content_type=ContentType.BILL,
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
        Sync all bills from Webflow.

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
            "Starting bill batch sync",
            limit=options.limit if options.limit > 0 else "unlimited",
            jurisdiction=options.jurisdiction or "all",
            include_pdfs=options.include_pdfs,
            include_openstates=options.include_openstates,
        )

        try:
            # Fetch raw items first to enable jurisdiction filtering
            async with httpx.AsyncClient(timeout=60.0) as client:
                headers = {
                    "Authorization": f"Bearer {self.settings.webflow_votebot_api_key.get_secret_value()}",
                    "accept": "application/json",
                }

                # Build organization mapping
                await self.webflow._build_organization_mapping(client, headers)

                offset = 0
                page_size = 100
                raw_items = []

                while True:
                    params = {"limit": page_size, "offset": offset}
                    response = await client.get(
                        f"https://api.webflow.com/v2/collections/{self.settings.webflow_bills_collection_id}/items",
                        headers=headers,
                        params=params,
                    )
                    response.raise_for_status()
                    data = response.json()
                    items = data.get("items", [])

                    if not items:
                        break

                    raw_items.extend(items)

                    pagination = data.get("pagination", {})
                    if offset + len(items) >= pagination.get("total", 0):
                        break

                    offset += page_size

            logger.info(f"Fetched {len(raw_items)} total bills from Webflow")

            # Filter by jurisdiction if specified
            if options.jurisdiction:
                jurisdiction_upper = options.jurisdiction.upper()
                filtered_items = []
                for item in raw_items:
                    fields = item.get("fieldData", {})
                    item_jurisdiction_id = fields.get("jurisdiction", "")
                    item_url = fields.get("open-states-url-2", "")
                    code = self.bill_sync.resolve_jurisdiction_code(
                        item_jurisdiction_id, item_url
                    )
                    if code.upper() == jurisdiction_upper:
                        filtered_items.append(item)

                logger.info(
                    f"Filtered to {len(filtered_items)} bills for jurisdiction {jurisdiction_upper}"
                )
                raw_items = filtered_items

            # Apply limit after filtering
            if options.limit > 0:
                raw_items = raw_items[:options.limit]

            # Process bills through webflow source
            bills = []
            for item in raw_items:
                async for doc in self.webflow._process_bill_item(
                    item, include_pdfs=options.include_pdfs
                ):
                    bills.append(doc)

            logger.info(f"Processed {len(bills)} bill documents")

            if options.dry_run:
                return SyncResult(
                    success=True,
                    content_type=ContentType.BILL,
                    mode=SyncMode.BATCH,
                    items_processed=len(bills),
                    items_successful=len(bills),
                    duration_seconds=time.perf_counter() - start_time,
                )

            # Ingest to vector store
            result = await self.pipeline.ingest_batch(bills)

            total_processed = len(bills)
            total_successful = result.documents_processed
            total_chunks = result.chunks_created
            errors.extend(result.errors)

            # Also sync OpenStates history for bills if enabled
            # Use the already-filtered raw_items from above
            if options.include_openstates and raw_items:
                os_result = await self.bill_sync.sync_current_session_bills(raw_items)
                total_chunks += os_result.chunks_created
                errors.extend(os_result.errors)

                # Chain bill version check — updates gov-url, status, status-date in Webflow CMS
                from votebot.updates.bill_version_sync import BillVersionSyncService
                version_sync = BillVersionSyncService(self.settings)
                version_result = await version_sync.sync_bill_versions(raw_items)
                total_chunks += version_result.chunks_created
                errors.extend(version_result.errors)

                if version_result.status_updates > 0 or version_result.updated > 0:
                    logger.info(
                        "Bill version sync included in batch",
                        version_checked=version_result.checked,
                        version_updated=version_result.updated,
                        status_updates=version_result.status_updates,
                        webflow_updates=version_result.webflow_updates,
                    )

            duration = time.perf_counter() - start_time
            success = total_successful > 0

            logger.info(
                "Bill batch sync complete",
                processed=total_processed,
                successful=total_successful,
                chunks_created=total_chunks,
                duration_seconds=round(duration, 2),
            )

            return SyncResult(
                success=success,
                content_type=ContentType.BILL,
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
            logger.exception("Bill batch sync failed", error=str(e))
            return SyncResult(
                success=False,
                content_type=ContentType.BILL,
                mode=SyncMode.BATCH,
                items_processed=total_processed,
                items_successful=total_successful,
                items_failed=total_processed - total_successful,
                chunks_created=total_chunks,
                errors=[str(e)] + errors,
                duration_seconds=time.perf_counter() - start_time,
            )
