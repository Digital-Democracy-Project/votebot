"""Document ingestion pipeline for VoteBot."""

from votebot.ingestion.chunking import ChunkingService
from votebot.ingestion.metadata import MetadataExtractor
from votebot.ingestion.pipeline import IngestionPipeline

__all__ = ["IngestionPipeline", "ChunkingService", "MetadataExtractor"]
