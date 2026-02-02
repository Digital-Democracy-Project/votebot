"""Base handler protocol for content type handlers."""

from typing import Protocol

from votebot.sync.types import ContentType, SyncIdentifier, SyncOptions, SyncResult


class ContentHandler(Protocol):
    """
    Protocol defining the interface for content type handlers.

    Each handler implements sync operations for a specific content type
    (bills, legislators, organizations, webpages, training docs).
    """

    @property
    def content_type(self) -> ContentType:
        """Return the content type this handler manages."""
        ...

    async def sync_single(
        self,
        identifier: SyncIdentifier,
        options: SyncOptions,
    ) -> SyncResult:
        """
        Sync a single item.

        Args:
            identifier: Identifier for the item to sync
            options: Sync options

        Returns:
            SyncResult with operation status and stats
        """
        ...

    async def sync_batch(
        self,
        options: SyncOptions,
    ) -> SyncResult:
        """
        Sync all items of this content type.

        Args:
            options: Sync options (may include limit, jurisdiction filters)

        Returns:
            SyncResult with aggregated operation stats
        """
        ...
