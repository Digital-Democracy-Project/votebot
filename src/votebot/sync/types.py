"""Type definitions for the unified sync service."""

from dataclasses import dataclass, field
from enum import Enum


class ContentType(str, Enum):
    """Supported content types for synchronization."""

    BILL = "bill"
    LEGISLATOR = "legislator"
    ORGANIZATION = "organization"
    WEBPAGE = "webpage"
    TRAINING = "training"


class SyncMode(str, Enum):
    """Sync operation mode."""

    SINGLE = "single"
    BATCH = "batch"


@dataclass
class SyncIdentifier:
    """
    Unified identifier for sync targets.

    At least one identifier must be set depending on content type:
    - Bills: webflow_id, slug, or openstates_id
    - Legislators: webflow_id, slug, or openstates_id
    - Organizations: webflow_id or slug
    - Webpages: url
    - Training docs: file_path
    """

    webflow_id: str | None = None
    slug: str | None = None
    openstates_id: str | None = None
    url: str | None = None
    file_path: str | None = None

    def __post_init__(self) -> None:
        """Validate that at least one identifier is set."""
        if not any([
            self.webflow_id,
            self.slug,
            self.openstates_id,
            self.url,
            self.file_path,
        ]):
            raise ValueError("At least one identifier must be provided")

    @property
    def primary_identifier(self) -> str:
        """Return the first available identifier for logging."""
        return (
            self.webflow_id
            or self.slug
            or self.openstates_id
            or self.url
            or self.file_path
            or "unknown"
        )


@dataclass
class SyncOptions:
    """
    Options for sync operations.

    Different content types use different options:
    - Bills: include_pdfs, include_openstates (votes are now included automatically)
    - Legislators: include_sponsored_bills
    - All batch operations: limit, jurisdiction

    Note: Vote syncing strategy has changed. Votes are now:
    - Synced per-bill during bill sync (creates bill-votes-{id} documents)
    - Available on-demand via BillVotesService for bills not in our system
    """

    # PDF processing for bills
    include_pdfs: bool = True

    # OpenStates integration (includes votes automatically)
    include_openstates: bool = True

    # Legislator-specific
    include_sponsored_bills: bool = True

    # Legacy vote options (deprecated - votes now sync with bills)
    include_votes: bool = False  # Deprecated: votes sync automatically with bills
    vote_session: str | None = None  # Deprecated
    max_vote_bills: int = 200  # Deprecated

    # Filtering
    jurisdiction: str | None = None

    # Batch limits
    limit: int = 0  # 0 = unlimited

    # Preview mode
    dry_run: bool = False


@dataclass
class SyncResult:
    """Result of a sync operation."""

    success: bool
    content_type: ContentType
    mode: SyncMode
    items_processed: int = 0
    items_successful: int = 0
    items_failed: int = 0
    chunks_created: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)
    document_ids: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        """Generate a human-readable summary of the sync result."""
        if self.mode == SyncMode.SINGLE:
            status = "succeeded" if self.success else "failed"
            return (
                f"{self.content_type.value} sync {status}: "
                f"{self.chunks_created} chunks created"
            )
        else:
            return (
                f"{self.content_type.value} batch sync: "
                f"{self.items_successful}/{self.items_processed} succeeded, "
                f"{self.chunks_created} chunks created"
            )
