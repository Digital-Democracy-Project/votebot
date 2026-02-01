"""Schemas for single-item sync API endpoints."""

from pydantic import BaseModel, Field


class SyncRequest(BaseModel):
    """Request schema for syncing a single item from Webflow."""

    webflow_item_id: str | None = Field(
        None,
        description="Primary identifier: Webflow CMS item ID",
    )
    slug: str | None = Field(
        None,
        description="Fallback identifier: Item slug in Webflow",
    )

    def model_post_init(self, __context) -> None:
        """Validate that at least one identifier is provided."""
        if not self.webflow_item_id and not self.slug:
            raise ValueError("Either webflow_item_id or slug must be provided")


class BillSyncRequest(SyncRequest):
    """Request schema for syncing a single bill."""

    pass


class LegislatorSyncRequest(SyncRequest):
    """Request schema for syncing a single legislator."""

    pass


class OrganizationSyncRequest(SyncRequest):
    """Request schema for syncing a single organization."""

    pass


class SyncResponse(BaseModel):
    """Response schema for sync operations."""

    success: bool = Field(..., description="Whether the sync operation succeeded")
    item_type: str = Field(..., description="Type of item synced (bill, legislator, organization)")
    item_id: str = Field(..., description="Webflow item ID that was synced")
    document_id: str | None = Field(None, description="Document ID created in Pinecone")
    chunks_created: int = Field(0, description="Number of chunks created in vector store")
    error: str | None = Field(None, description="Error message if sync failed")
    duration_ms: int = Field(0, description="Duration of sync operation in milliseconds")
