"""Unified sync endpoint for all content types."""

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from votebot.api.middleware.auth import api_key_auth
from votebot.config import Settings, get_settings
from votebot.services.redis_store import get_redis_store
from votebot.sync import (
    ContentType,
    SyncIdentifier,
    SyncMode,
    SyncOptions,
    UnifiedSyncService,
)

# Store for tracking background sync tasks
_background_tasks: dict[str, dict] = {}

router = APIRouter(prefix="/sync", tags=["sync"])
logger = structlog.get_logger()


class UnifiedSyncRequest(BaseModel):
    """Request schema for unified sync endpoint."""

    content_type: str = Field(
        ...,
        description="Content type to sync: bill, legislator, organization, webpage, training",
    )
    mode: str = Field(
        default="single",
        description="Sync mode: single or batch",
    )

    # Identifiers (at least one required for single mode)
    webflow_id: str | None = Field(
        None,
        description="Webflow CMS item ID",
    )
    slug: str | None = Field(
        None,
        description="Item slug in Webflow",
    )
    openstates_id: str | None = Field(
        None,
        description="OpenStates ID (for bills/legislators)",
    )
    url: str | None = Field(
        None,
        description="URL (for webpages)",
    )
    file_path: str | None = Field(
        None,
        description="File path (for training documents)",
    )

    # Options
    include_pdfs: bool = Field(
        default=True,
        description="Include PDF processing for bills",
    )
    include_openstates: bool = Field(
        default=True,
        description="Include OpenStates data for bills",
    )
    include_sponsored_bills: bool = Field(
        default=True,
        description="Include sponsored bills for legislators",
    )
    jurisdiction: str | None = Field(
        None,
        description="Filter by jurisdiction (e.g., FL, US)",
    )
    limit: int = Field(
        default=0,
        ge=0,
        description="Maximum items to process (0 = unlimited)",
    )
    dry_run: bool = Field(
        default=False,
        description="Preview without ingesting",
    )
    resume_task_id: str | None = Field(
        default=None,
        description="Task ID of a previous run to resume from (skips already-processed items)",
    )

    @model_validator(mode="after")
    def validate_identifier_for_single_mode(self) -> "UnifiedSyncRequest":
        """Validate that an identifier is provided for single mode."""
        if self.mode == "single":
            has_identifier = any([
                self.webflow_id,
                self.slug,
                self.openstates_id,
                self.url,
                self.file_path,
            ])
            if not has_identifier:
                raise ValueError(
                    "At least one identifier (webflow_id, slug, openstates_id, url, or file_path) "
                    "is required for single mode"
                )
        return self


class UnifiedSyncResponse(BaseModel):
    """Response schema for unified sync endpoint."""

    success: bool = Field(..., description="Whether the sync operation succeeded")
    content_type: str = Field(..., description="Content type that was synced")
    mode: str = Field(..., description="Sync mode used")
    items_processed: int = Field(default=0, description="Number of items processed")
    items_successful: int = Field(default=0, description="Number of items successfully synced")
    items_failed: int = Field(default=0, description="Number of items that failed")
    chunks_created: int = Field(default=0, description="Number of chunks created in vector store")
    duration_ms: int = Field(default=0, description="Duration of sync operation in milliseconds")
    errors: list[str] = Field(default_factory=list, description="List of error messages")
    document_ids: list[str] = Field(default_factory=list, description="IDs of documents created")
    # For async batch operations
    task_id: str | None = Field(default=None, description="Background task ID (for batch operations)")
    status: str = Field(default="completed", description="Task status: accepted, running, completed, failed")


async def _run_batch_sync_background(
    task_id: str,
    content_type: ContentType,
    options: SyncOptions,
    settings: Settings,
) -> None:
    """Run batch sync in the background and update task status."""
    redis_store = get_redis_store()
    start_time = time.perf_counter()

    # Initialize live result dict so the status endpoint returns real-time counts
    live_result: dict = {
        "success": False,
        "items_processed": 0,
        "items_successful": 0,
        "items_failed": 0,
        "chunks_created": 0,
        "duration_ms": 0,
        "errors": [],
        "document_ids": [],
    }
    _background_tasks[task_id]["status"] = "running"
    _background_tasks[task_id]["result"] = live_result
    await redis_store.set_sync_task(task_id, _background_tasks[task_id])

    # Counter for throttling Redis writes
    _redis_write_counter = 0

    async def _progress(
        items_processed: int,
        items_successful: int,
        items_failed: int,
        chunks_created: int,
        errors: list[str] | None = None,
    ) -> None:
        """Update live progress — called by handlers after each item."""
        nonlocal _redis_write_counter
        live_result["items_processed"] = items_processed
        live_result["items_successful"] = items_successful
        live_result["items_failed"] = items_failed
        live_result["chunks_created"] = chunks_created
        live_result["duration_ms"] = int((time.perf_counter() - start_time) * 1000)
        if errors:
            live_result["errors"] = errors

        # Update heartbeat for zombie detection
        _background_tasks[task_id]["last_heartbeat"] = datetime.now(timezone.utc).isoformat()

        # Throttle Redis writes to every 10 progress calls
        _redis_write_counter += 1
        if _redis_write_counter % 10 == 0:
            await redis_store.set_sync_task(task_id, _background_tasks[task_id])

    # Wire progress callback and task_id into options
    options.progress_callback = _progress
    options.task_id = task_id

    try:
        service = UnifiedSyncService(settings)
        result = await service.sync(
            content_type=content_type,
            mode=SyncMode.BATCH,
            identifier=None,
            options=options,
        )

        duration_ms = int((time.perf_counter() - start_time) * 1000)

        _background_tasks[task_id].update({
            "status": "completed" if result.success else "failed",
            "result": {
                "success": result.success,
                "items_processed": result.items_processed,
                "items_successful": result.items_successful,
                "items_failed": result.items_failed,
                "chunks_created": result.chunks_created,
                "duration_ms": duration_ms,
                "errors": result.errors,
                "document_ids": result.document_ids,
            },
        })
        await redis_store.set_sync_task(task_id, _background_tasks[task_id])

        logger.info(
            "Background batch sync complete",
            task_id=task_id,
            content_type=content_type.value,
            success=result.success,
            items_processed=result.items_processed,
            chunks_created=result.chunks_created,
            duration_ms=duration_ms,
        )

    except Exception as e:
        logger.exception("Background batch sync failed", task_id=task_id, error=str(e))
        _background_tasks[task_id].update({
            "status": "failed",
            "result": {
                "success": False,
                "items_processed": live_result["items_processed"],
                "items_successful": live_result["items_successful"],
                "items_failed": live_result["items_failed"],
                "chunks_created": live_result["chunks_created"],
                "duration_ms": int((time.perf_counter() - start_time) * 1000),
                "errors": live_result["errors"] + [str(e)],
                "document_ids": [],
            },
        })
        await redis_store.set_sync_task(task_id, _background_tasks[task_id])


@router.post(
    "/unified",
    response_model=UnifiedSyncResponse,
    summary="Unified sync endpoint for all content types",
    description="Sync bills, legislators, organizations, webpages, or training documents.",
)
async def sync_unified(
    request: UnifiedSyncRequest,
    api_key: Annotated[str, Depends(api_key_auth)],
    settings: Settings = Depends(get_settings),
) -> UnifiedSyncResponse:
    """
    Unified sync endpoint for all content types.

    Supports:
    - Bills: Single by webflow_id/slug, batch with optional PDF/OpenStates
    - Legislators: Single by webflow_id/slug, batch with optional sponsored bills
    - Organizations: Single by webflow_id/slug, batch
    - Webpages: Single by URL only (no batch support)
    - Training: Single by file_path only (no batch support via API)

    For batch mode, the sync runs in the background and returns immediately
    with a task_id. Use GET /sync/unified/status/{task_id} to check progress.
    """
    start_time = time.perf_counter()

    logger.info(
        "Unified sync request",
        content_type=request.content_type,
        mode=request.mode,
        dry_run=request.dry_run,
    )

    try:
        # Parse content type
        try:
            content_type = ContentType(request.content_type.lower())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid content type: {request.content_type}. "
                f"Valid types: {', '.join(ct.value for ct in ContentType)}",
            )

        # Parse sync mode
        try:
            mode = SyncMode(request.mode.lower())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid mode: {request.mode}. Valid modes: single, batch",
            )

        # Build identifier for single mode
        identifier = None
        if mode == SyncMode.SINGLE:
            try:
                identifier = SyncIdentifier(
                    webflow_id=request.webflow_id,
                    slug=request.slug,
                    openstates_id=request.openstates_id,
                    url=request.url,
                    file_path=request.file_path,
                )
            except ValueError as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=str(e),
                )

        # Build options
        options = SyncOptions(
            include_pdfs=request.include_pdfs,
            include_openstates=request.include_openstates,
            include_sponsored_bills=request.include_sponsored_bills,
            jurisdiction=request.jurisdiction,
            limit=request.limit,
            dry_run=request.dry_run,
        )

        # For batch mode, run in background and return immediately
        if mode == SyncMode.BATCH:
            task_id = str(uuid.uuid4())
            _background_tasks[task_id] = {
                "status": "accepted",
                "content_type": content_type.value,
                "mode": "batch",
                "started_at": time.time(),
                "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                "retry_count": 0,
                "options": {
                    "include_pdfs": options.include_pdfs,
                    "include_openstates": options.include_openstates,
                    "include_sponsored_bills": options.include_sponsored_bills,
                    "jurisdiction": options.jurisdiction,
                    "limit": options.limit,
                    "dry_run": options.dry_run,
                },
            }

            # Resume support: copy checkpoints from a previous task
            if request.resume_task_id:
                options.resume_task_id = request.resume_task_id
                redis_store = get_redis_store()
                copied = await redis_store.copy_sync_checkpoints(
                    request.resume_task_id, task_id
                )
                logger.info(
                    "Resuming from previous task",
                    resume_task_id=request.resume_task_id,
                    new_task_id=task_id,
                    checkpoints_copied=copied,
                )

            # Write-through to Redis for cross-worker visibility
            redis_store = get_redis_store()
            await redis_store.set_sync_task(task_id, _background_tasks[task_id])

            # Start background task
            asyncio.create_task(
                _run_batch_sync_background(task_id, content_type, options, settings)
            )

            logger.info(
                "Batch sync started in background",
                task_id=task_id,
                content_type=content_type.value,
            )

            return UnifiedSyncResponse(
                success=True,
                content_type=content_type.value,
                mode=mode.value,
                status="accepted",
                task_id=task_id,
                duration_ms=int((time.perf_counter() - start_time) * 1000),
            )

        # Execute sync synchronously for single mode
        service = UnifiedSyncService(settings)
        result = await service.sync(
            content_type=content_type,
            mode=mode,
            identifier=identifier,
            options=options,
        )

        duration_ms = int((time.perf_counter() - start_time) * 1000)

        logger.info(
            "Unified sync complete",
            content_type=content_type.value,
            mode=mode.value,
            success=result.success,
            items_processed=result.items_processed,
            chunks_created=result.chunks_created,
            duration_ms=duration_ms,
        )

        return UnifiedSyncResponse(
            success=result.success,
            content_type=result.content_type.value,
            mode=result.mode.value,
            items_processed=result.items_processed,
            items_successful=result.items_successful,
            items_failed=result.items_failed,
            chunks_created=result.chunks_created,
            duration_ms=duration_ms,
            errors=result.errors,
            document_ids=result.document_ids,
            status="completed",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unified sync failed", error=str(e))
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        return UnifiedSyncResponse(
            success=False,
            content_type=request.content_type,
            mode=request.mode,
            duration_ms=duration_ms,
            errors=[str(e)],
            status="failed",
        )


@router.get(
    "/unified/status/{task_id}",
    response_model=UnifiedSyncResponse,
    summary="Get status of a background sync task",
    description="Check the status of a batch sync operation started in the background.",
)
async def get_sync_status(
    task_id: str,
    api_key: Annotated[str, Depends(api_key_auth)],
) -> UnifiedSyncResponse:
    """
    Get the status of a background sync task.

    Returns the current status and results (if completed) of a batch sync operation.
    """
    task = _background_tasks.get(task_id)
    if task is None:
        # Cross-worker fallback: check Redis
        redis_store = get_redis_store()
        task = await redis_store.get_sync_task(task_id)

    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task not found: {task_id}",
        )
    result = task.get("result", {})

    return UnifiedSyncResponse(
        success=result.get("success", task["status"] == "completed"),
        content_type=task["content_type"],
        mode=task["mode"],
        status=task["status"],
        task_id=task_id,
        items_processed=result.get("items_processed", 0),
        items_successful=result.get("items_successful", 0),
        items_failed=result.get("items_failed", 0),
        chunks_created=result.get("chunks_created", 0),
        duration_ms=result.get("duration_ms", 0),
        errors=result.get("errors", []),
        document_ids=result.get("document_ids", []),
    )


@router.post(
    "/unified/all",
    response_model=dict[str, UnifiedSyncResponse],
    summary="Sync all content types in batch mode",
    description="Run batch sync for bills, legislators, and organizations.",
)
async def sync_all(
    api_key: Annotated[str, Depends(api_key_auth)],
    settings: Settings = Depends(get_settings),
    include_pdfs: bool = True,
    include_openstates: bool = True,
    include_sponsored_bills: bool = True,
    limit: int = 0,
    dry_run: bool = False,
) -> dict[str, UnifiedSyncResponse]:
    """
    Sync all content types in batch mode.

    Syncs bills, legislators, and organizations from Webflow to Pinecone.
    """
    start_time = time.perf_counter()

    logger.info(
        "Sync all request",
        include_pdfs=include_pdfs,
        include_openstates=include_openstates,
        include_sponsored_bills=include_sponsored_bills,
        limit=limit,
        dry_run=dry_run,
    )

    options = SyncOptions(
        include_pdfs=include_pdfs,
        include_openstates=include_openstates,
        include_sponsored_bills=include_sponsored_bills,
        limit=limit,
        dry_run=dry_run,
    )

    service = UnifiedSyncService(settings)
    results = await service.sync_all(options)

    duration_ms = int((time.perf_counter() - start_time) * 1000)

    # Convert to response format
    response = {}
    for content_type, result in results.items():
        response[content_type.value] = UnifiedSyncResponse(
            success=result.success,
            content_type=result.content_type.value,
            mode=result.mode.value,
            items_processed=result.items_processed,
            items_successful=result.items_successful,
            items_failed=result.items_failed,
            chunks_created=result.chunks_created,
            duration_ms=int(result.duration_seconds * 1000),
            errors=result.errors,
            document_ids=result.document_ids,
        )

    logger.info(
        "Sync all complete",
        total_duration_ms=duration_ms,
        results={k: v.success for k, v in response.items()},
    )

    return response
