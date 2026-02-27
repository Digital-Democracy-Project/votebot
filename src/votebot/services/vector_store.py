"""Pinecone vector store service."""

from dataclasses import dataclass, field
from typing import Any

import structlog
from pinecone import Pinecone, ServerlessSpec
from tenacity import retry, stop_after_attempt, wait_exponential

from votebot.config import Settings, get_settings
from votebot.services.embeddings import EmbeddingService

logger = structlog.get_logger()


@dataclass
class Document:
    """A document to be stored in the vector store."""

    id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None


@dataclass
class SearchResult:
    """A search result from the vector store."""

    id: str
    content: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


class VectorStoreService:
    """Service for interacting with Pinecone vector store."""

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the vector store service.

        Args:
            settings: Application settings. Uses default if not provided.
        """
        self.settings = settings or get_settings()
        self.pc = Pinecone(api_key=self.settings.pinecone_api_key.get_secret_value())
        self.index_name = self.settings.pinecone_index_name
        self.namespace = self.settings.pinecone_namespace
        self._index = None
        self.embedding_service = EmbeddingService(self.settings)

    @property
    def index(self):
        """Get or create the Pinecone index."""
        if self._index is None:
            # Check if index exists
            existing_indexes = [idx.name for idx in self.pc.list_indexes()]

            if self.index_name not in existing_indexes:
                logger.info("Creating Pinecone index", index_name=self.index_name)
                self.pc.create_index(
                    name=self.index_name,
                    dimension=EmbeddingService.get_dimension(),
                    metric="cosine",
                    spec=ServerlessSpec(
                        cloud="aws",
                        region=self.settings.pinecone_environment,
                    ),
                )

            self._index = self.pc.Index(self.index_name)
        return self._index

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    async def upsert_documents(
        self,
        documents: list[Document],
        batch_size: int = 100,
    ) -> int:
        """
        Add or update documents in the vector store.

        Args:
            documents: List of documents to upsert
            batch_size: Number of documents per batch

        Returns:
            Number of documents upserted
        """
        # Generate embeddings for documents without them
        texts_to_embed = []
        docs_needing_embeddings = []

        for doc in documents:
            if doc.embedding is None:
                texts_to_embed.append(doc.content)
                docs_needing_embeddings.append(doc)

        if texts_to_embed:
            embeddings = await self.embedding_service.embed_documents(texts_to_embed)
            for doc, embedding in zip(docs_needing_embeddings, embeddings):
                doc.embedding = embedding

        # Build vectors and upsert incrementally to cap memory at one batch
        total_upserted = 0
        batch = []
        batch_index = 0
        for doc in documents:
            batch.append(
                {
                    "id": doc.id,
                    "values": doc.embedding,
                    "metadata": {
                        "content": doc.content[:40000],  # Pinecone metadata limit
                        **doc.metadata,
                    },
                }
            )
            if len(batch) >= batch_size:
                logger.debug(
                    "Upserting batch to Pinecone",
                    batch_size=len(batch),
                    batch_index=batch_index,
                )
                self.index.upsert(vectors=batch, namespace=self.namespace)
                total_upserted += len(batch)
                batch = []
                batch_index += 1

        # Flush remaining vectors
        if batch:
            logger.debug(
                "Upserting batch to Pinecone",
                batch_size=len(batch),
                batch_index=batch_index,
            )
            self.index.upsert(vectors=batch, namespace=self.namespace)
            total_upserted += len(batch)

        logger.info(
            "Documents upserted to vector store",
            count=total_upserted,
            index=self.index_name,
        )

        return total_upserted

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    async def query(
        self,
        query: str,
        top_k: int = 10,
        filter: dict[str, Any] | None = None,
        include_metadata: bool = True,
    ) -> list[SearchResult]:
        """
        Search for similar documents.

        Args:
            query: The search query
            top_k: Maximum number of results to return
            filter: Optional metadata filter
            include_metadata: Whether to include metadata in results

        Returns:
            List of SearchResult objects
        """
        # Generate query embedding
        query_embedding = await self.embedding_service.embed_query(query)

        logger.info(
            "Querying Pinecone",
            top_k=top_k,
            filter=filter,
        )

        # Query Pinecone
        results = self.index.query(
            vector=query_embedding,
            top_k=top_k,
            filter=filter,
            include_metadata=include_metadata,
            namespace=self.namespace,
        )

        # Convert to SearchResult objects
        search_results = []
        for match in results.matches:
            metadata = match.metadata or {}
            content = metadata.pop("content", "")

            search_results.append(
                SearchResult(
                    id=match.id,
                    content=content,
                    score=match.score,
                    metadata=metadata,
                )
            )

        # Log results with scores for debugging
        scores = [r.score for r in search_results]
        logger.info(
            "Pinecone query completed",
            result_count=len(search_results),
            top_scores=scores[:5] if scores else [],
        )

        return search_results

    async def query_with_filter(
        self,
        query: str,
        document_type: str | None = None,
        bill_id: str | None = None,
        legislator_id: str | None = None,
        jurisdiction: str | None = None,
        top_k: int = 10,
    ) -> list[SearchResult]:
        """
        Search with common metadata filters.

        Args:
            query: The search query
            document_type: Filter by document type
            bill_id: Filter by bill ID
            legislator_id: Filter by legislator ID
            jurisdiction: Filter by jurisdiction
            top_k: Maximum number of results

        Returns:
            List of SearchResult objects
        """
        # Build filter
        filter_dict = {}
        if document_type:
            filter_dict["document_type"] = document_type
        if bill_id:
            filter_dict["bill_id"] = bill_id
        if legislator_id:
            filter_dict["legislator_id"] = legislator_id
        if jurisdiction:
            filter_dict["jurisdiction"] = jurisdiction

        return await self.query(
            query=query,
            top_k=top_k,
            filter=filter_dict if filter_dict else None,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    async def delete(
        self,
        ids: list[str] | None = None,
        filter: dict[str, Any] | None = None,
        delete_all: bool = False,
    ) -> None:
        """
        Delete documents from the vector store.

        Args:
            ids: List of document IDs to delete
            filter: Metadata filter for deletion
            delete_all: Delete all documents in namespace
        """
        if delete_all:
            logger.warning(
                "Deleting all documents from namespace",
                namespace=self.namespace,
            )
            self.index.delete(delete_all=True, namespace=self.namespace)
        elif ids:
            logger.info(
                "Deleting documents by ID",
                count=len(ids),
            )
            self.index.delete(ids=ids, namespace=self.namespace)
        elif filter:
            logger.info(
                "Deleting documents by filter",
                filter=filter,
            )
            self.index.delete(filter=filter, namespace=self.namespace)

    async def health_check(self) -> bool:
        """
        Check if Pinecone is accessible.

        Returns:
            True if healthy, raises exception otherwise
        """
        try:
            # Describe index to verify connectivity
            stats = self.index.describe_index_stats()
            logger.debug(
                "Pinecone health check passed",
                total_vectors=stats.total_vector_count,
            )
            return True
        except Exception as e:
            logger.error("Pinecone health check failed", error=str(e))
            raise


class VectorStoreServiceFactory:
    """Factory for creating vector store service instances."""

    _instance: VectorStoreService | None = None

    @classmethod
    def get_instance(cls, settings: Settings | None = None) -> VectorStoreService:
        """Get or create a singleton vector store service instance."""
        if cls._instance is None:
            cls._instance = VectorStoreService(settings)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton instance (useful for testing)."""
        cls._instance = None
