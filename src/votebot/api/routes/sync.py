"""Sync endpoints for individual item synchronization from Webflow to Pinecone."""

import time
from typing import Annotated

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, status

from votebot.api.middleware.auth import api_key_auth
from votebot.api.schemas.sync import (
    BillSyncRequest,
    LegislatorSyncRequest,
    OrganizationSyncRequest,
    SyncResponse,
)
from votebot.config import Settings, get_settings
from votebot.ingestion.pipeline import IngestionPipeline
from votebot.ingestion.sources.webflow import WebflowSource
from votebot.updates.bill_sync import BillSyncService
from votebot.updates.legislator_sync import LegislatorSyncService
from votebot.updates.organization_sync import OrganizationSyncService

router = APIRouter(prefix="/sync", tags=["sync"])
logger = structlog.get_logger()


async def _get_webflow_item(
    webflow: WebflowSource,
    collection_id: str,
    webflow_item_id: str | None,
    slug: str | None,
) -> tuple[dict | None, str]:
    """
    Fetch a Webflow item by ID or slug.

    Args:
        webflow: WebflowSource instance
        collection_id: Webflow collection ID
        webflow_item_id: Item ID (preferred)
        slug: Item slug (fallback)

    Returns:
        Tuple of (item_data, item_id)
    """
    if webflow_item_id:
        item = await webflow.fetch_item_by_id(collection_id, webflow_item_id)
        return item, webflow_item_id
    elif slug:
        item = await webflow.fetch_item_by_slug(collection_id, slug)
        if item:
            return item, item.get("id", "")
        return None, ""
    return None, ""


@router.post(
    "/bill",
    response_model=SyncResponse,
    summary="Sync a single bill from Webflow to Pinecone",
    description="Fetch a bill from Webflow CMS and sync its content to the vector store.",
)
async def sync_bill(
    request: BillSyncRequest,
    api_key: Annotated[str, Depends(api_key_auth)],
    settings: Settings = Depends(get_settings),
) -> SyncResponse:
    """
    Sync a single bill from Webflow CMS to Pinecone.

    This endpoint:
    1. Fetches the bill from Webflow by ID or slug
    2. Processes the bill content (CMS fields)
    3. Fetches legislative history from OpenStates if available
    4. Ingests the content to the vector store
    """
    start_time = time.perf_counter()

    logger.info(
        "Syncing bill",
        webflow_item_id=request.webflow_item_id,
        slug=request.slug,
    )

    try:
        webflow = WebflowSource(settings)
        collection_id = settings.webflow_bills_collection_id

        if not collection_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Bills collection ID not configured",
            )

        # Fetch the item from Webflow
        item, item_id = await _get_webflow_item(
            webflow, collection_id, request.webflow_item_id, request.slug
        )

        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Bill not found in Webflow: {request.webflow_item_id or request.slug}",
            )

        fields = item.get("fieldData", {})
        title = fields.get("name", "Unknown Bill")
        openstates_url = fields.get("open-states-url-2", "")
        jurisdiction_id = fields.get("jurisdiction", "")
        slug = fields.get("slug", "")

        chunks_created = 0
        document_id = ""

        # If OpenStates URL is available, sync legislative history
        if openstates_url:
            bill_sync = BillSyncService(settings)

            # Get jurisdiction code from the mapping
            jurisdiction_code = bill_sync.JURISDICTION_MAP.get(jurisdiction_id, "")

            result = await bill_sync.sync_bill(
                openstates_url=openstates_url,
                webflow_bill_id=item_id,
                bill_title=title,
                jurisdiction_name=jurisdiction_code,
                bill_slug=slug,
            )

            if result.success:
                chunks_created += result.chunks_created
                document_id = f"bill-history-{item_id}"
            else:
                logger.warning(
                    "Failed to sync bill from OpenStates",
                    item_id=item_id,
                    error=result.error,
                )

        # Also ingest the Webflow CMS content
        pipeline = IngestionPipeline(settings)

        # Build organization mapping for bill content
        async with httpx.AsyncClient(timeout=60.0) as client:
            headers = {
                "Authorization": f"Bearer {settings.webflow_api_key.get_secret_value()}",
                "accept": "application/json",
            }
            await webflow._build_organization_mapping(client, headers)

        # Process the bill item to get CMS content
        async for doc in webflow._process_bill_item(item, include_pdfs=False):
            cms_result = await pipeline.ingest_document(
                content=doc.content,
                metadata=doc.metadata,
                skip_duplicates=False,
            )
            chunks_created += cms_result.chunks_created
            if not document_id:
                document_id = doc.metadata.document_id

        duration_ms = int((time.perf_counter() - start_time) * 1000)

        logger.info(
            "Bill sync complete",
            item_id=item_id,
            chunks_created=chunks_created,
            duration_ms=duration_ms,
        )

        return SyncResponse(
            success=True,
            item_type="bill",
            item_id=item_id,
            document_id=document_id,
            chunks_created=chunks_created,
            duration_ms=duration_ms,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error syncing bill", error=str(e))
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        return SyncResponse(
            success=False,
            item_type="bill",
            item_id=request.webflow_item_id or "",
            error=str(e),
            duration_ms=duration_ms,
        )


@router.post(
    "/legislator",
    response_model=SyncResponse,
    summary="Sync a single legislator from Webflow to Pinecone",
    description="Fetch a legislator from Webflow CMS and sync their content to the vector store.",
)
async def sync_legislator(
    request: LegislatorSyncRequest,
    api_key: Annotated[str, Depends(api_key_auth)],
    settings: Settings = Depends(get_settings),
) -> SyncResponse:
    """
    Sync a single legislator from Webflow CMS to Pinecone.

    This endpoint:
    1. Fetches the legislator from Webflow by ID or slug
    2. Processes the legislator profile content
    3. Fetches sponsored bills from OpenStates
    4. Ingests the content to the vector store
    """
    start_time = time.perf_counter()

    logger.info(
        "Syncing legislator",
        webflow_item_id=request.webflow_item_id,
        slug=request.slug,
    )

    try:
        webflow = WebflowSource(settings)
        collection_id = settings.webflow_legislators_collection_id

        if not collection_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Legislators collection ID not configured",
            )

        # Fetch the item from Webflow
        item, item_id = await _get_webflow_item(
            webflow, collection_id, request.webflow_item_id, request.slug
        )

        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Legislator not found in Webflow: {request.webflow_item_id or request.slug}",
            )

        fields = item.get("fieldData", {})
        name = fields.get("name", "Unknown Legislator")
        openstates_id = fields.get("openstatesid", "")
        slug = fields.get("slug", "")
        party = fields.get("party-2", fields.get("party", ""))
        chamber = fields.get("chamber", "")

        # Resolve jurisdiction
        jurisdiction_ref = fields.get("jurisdiction")
        jurisdiction = "us"
        if isinstance(jurisdiction_ref, list) and jurisdiction_ref:
            jurisdiction_ref = jurisdiction_ref[0]
        if isinstance(jurisdiction_ref, str) and len(jurisdiction_ref) == 2:
            jurisdiction = jurisdiction_ref.lower()

        chunks_created = 0
        document_id = ""

        # Build jurisdiction mapping for Webflow content processing
        async with httpx.AsyncClient(timeout=60.0) as client:
            headers = {
                "Authorization": f"Bearer {settings.webflow_api_key.get_secret_value()}",
                "accept": "application/json",
            }
            await webflow._build_jurisdiction_mapping(client, headers)

        # Process the legislator item for Webflow content
        doc = webflow._process_legislator_item(item)
        if doc:
            pipeline = IngestionPipeline(settings)
            cms_result = await pipeline.ingest_document(
                content=doc.content,
                metadata=doc.metadata,
                skip_duplicates=False,
            )
            chunks_created += cms_result.chunks_created
            document_id = doc.metadata.document_id

        # Sync sponsored bills from OpenStates if ID is available
        if openstates_id:
            legislator_sync = LegislatorSyncService(settings)

            legislator_data = {
                "openstates_id": openstates_id,
                "name": name,
                "slug": slug,
                "jurisdiction": jurisdiction,
                "party": party,
                "chamber": chamber,
            }

            result = await legislator_sync.sync_legislator(legislator_data)

            if result.success:
                chunks_created += result.chunks_created
                if not document_id:
                    document_id = f"legislator-bills-{openstates_id}"
            else:
                logger.warning(
                    "Failed to sync legislator bills",
                    item_id=item_id,
                    error=result.error,
                )

        duration_ms = int((time.perf_counter() - start_time) * 1000)

        logger.info(
            "Legislator sync complete",
            item_id=item_id,
            chunks_created=chunks_created,
            duration_ms=duration_ms,
        )

        return SyncResponse(
            success=True,
            item_type="legislator",
            item_id=item_id,
            document_id=document_id,
            chunks_created=chunks_created,
            duration_ms=duration_ms,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error syncing legislator", error=str(e))
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        return SyncResponse(
            success=False,
            item_type="legislator",
            item_id=request.webflow_item_id or "",
            error=str(e),
            duration_ms=duration_ms,
        )


@router.post(
    "/organization",
    response_model=SyncResponse,
    summary="Sync a single organization from Webflow to Pinecone",
    description="Fetch an organization from Webflow CMS and sync its content to the vector store.",
)
async def sync_organization(
    request: OrganizationSyncRequest,
    api_key: Annotated[str, Depends(api_key_auth)],
    settings: Settings = Depends(get_settings),
) -> SyncResponse:
    """
    Sync a single organization from Webflow CMS to Pinecone.

    This endpoint:
    1. Fetches the organization from Webflow by ID or slug
    2. Resolves bill references for support/oppose positions
    3. Processes the organization profile content
    4. Ingests the content to the vector store
    """
    start_time = time.perf_counter()

    logger.info(
        "Syncing organization",
        webflow_item_id=request.webflow_item_id,
        slug=request.slug,
    )

    try:
        webflow = WebflowSource(settings)
        collection_id = settings.webflow_organizations_collection_id

        if not collection_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Organizations collection ID not configured",
            )

        # Fetch the item from Webflow
        item, item_id = await _get_webflow_item(
            webflow, collection_id, request.webflow_item_id, request.slug
        )

        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Organization not found in Webflow: {request.webflow_item_id or request.slug}",
            )

        fields = item.get("fieldData", {})
        name = fields.get("name", "Unknown Organization")

        # Use OrganizationSyncService for content processing
        org_sync = OrganizationSyncService(settings)
        result = await org_sync.sync_organization(item)

        duration_ms = int((time.perf_counter() - start_time) * 1000)

        if result.success:
            logger.info(
                "Organization sync complete",
                item_id=item_id,
                chunks_created=result.chunks_created,
                duration_ms=duration_ms,
            )

            return SyncResponse(
                success=True,
                item_type="organization",
                item_id=item_id,
                document_id=f"organization-{item_id}",
                chunks_created=result.chunks_created,
                duration_ms=duration_ms,
            )
        else:
            return SyncResponse(
                success=False,
                item_type="organization",
                item_id=item_id,
                error=result.error,
                duration_ms=duration_ms,
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error syncing organization", error=str(e))
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        return SyncResponse(
            success=False,
            item_type="organization",
            item_id=request.webflow_item_id or "",
            error=str(e),
            duration_ms=duration_ms,
        )
