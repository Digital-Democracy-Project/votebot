"""Embedding generation service using OpenAI."""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass

import structlog
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from votebot.config import Settings, get_settings

logger = structlog.get_logger()


@dataclass
class EmbeddingResult:
    """Result of an embedding operation."""

    embedding: list[float]
    tokens_used: int
    model: str


class EmbeddingService:
    """Service for generating text embeddings using OpenAI."""

    # Dimension for text-embedding-3-large
    EMBEDDING_DIMENSION = 3072

    # Query vectors kept in memory (VOTEBOT-46). One message makes several retrieval queries over the same text,
    # and a vector is ~100 KB as Python floats, so the bound is entries, not time: the same text always gives the
    # same vector. Memory only, never logged or written out.
    QUERY_CACHE_MAX = 128

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the embedding service.

        Args:
            settings: Application settings. Uses default if not provided.
        """
        self.settings = settings or get_settings()
        self.client = AsyncOpenAI(
            api_key=self.settings.openai_api_key.get_secret_value()
        )
        self.model = self.settings.openai_embedding_model
        # text -> the task embedding it (finished or still running); a finished one is the cache entry
        self._query_vectors: OrderedDict[str, asyncio.Task[EmbeddingResult]] = OrderedDict()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    async def embed(self, text: str) -> EmbeddingResult:
        """
        Generate an embedding for a single text.

        Args:
            text: The text to embed

        Returns:
            EmbeddingResult with the embedding vector
        """
        logger.debug(
            "Generating embedding",
            text_length=len(text),
            model=self.model,
        )

        response = await self.client.embeddings.create(
            model=self.model,
            input=text,
        )

        embedding = response.data[0].embedding
        tokens_used = response.usage.total_tokens if response.usage else 0

        logger.debug(
            "Embedding generated",
            dimension=len(embedding),
            tokens_used=tokens_used,
        )

        return EmbeddingResult(
            embedding=embedding,
            tokens_used=tokens_used,
            model=response.model,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    async def embed_batch(
        self,
        texts: list[str],
        batch_size: int = 100,
    ) -> list[EmbeddingResult]:
        """
        Generate embeddings for multiple texts.

        Args:
            texts: List of texts to embed
            batch_size: Maximum number of texts per API call

        Returns:
            List of EmbeddingResult objects
        """
        results = []

        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]

            logger.debug(
                "Generating batch embeddings",
                batch_size=len(batch),
                batch_index=i // batch_size,
            )

            response = await self.client.embeddings.create(
                model=self.model,
                input=batch,
            )

            tokens_used = response.usage.total_tokens if response.usage else 0

            for item in response.data:
                results.append(
                    EmbeddingResult(
                        embedding=item.embedding,
                        tokens_used=tokens_used // len(batch),  # Approximate per-item
                        model=response.model,
                    )
                )

        return results

    async def embed_query(self, query: str) -> list[float]:
        """
        Generate an embedding for a search query.

        This is a convenience method that returns just the embedding vector. The same text is embedded once:
        later calls reuse the vector, and calls made while it is still being fetched share that one request.
        The returned list is shared, so callers must not change it. A failed embedding is not kept.

        Args:
            query: The search query

        Returns:
            Embedding vector as list of floats
        """
        task = self._query_vectors.get(query)
        if task is None:
            task = asyncio.ensure_future(self.embed(query))
            self._query_vectors[query] = task
            task.add_done_callback(lambda t, q=query: self._settled(q, t))
            while len(self._query_vectors) > self.QUERY_CACHE_MAX:
                self._query_vectors.popitem(last=False)
        else:
            self._query_vectors.move_to_end(query)
            logger.debug("Query embedding reused", in_flight=not task.done())
        # shield: a caller that gives up (its time budget ran out) must not cancel the request others share
        return (await asyncio.shield(task)).embedding

    def _settled(self, query: str, task: "asyncio.Task[EmbeddingResult]") -> None:
        """Keep a successful embedding; drop a failed (or cancelled) one so the next call tries again."""
        failed = task.cancelled() or task.exception() is not None
        if failed and self._query_vectors.get(query) is task:
            del self._query_vectors[query]

    async def embed_documents(self, documents: list[str]) -> list[list[float]]:
        """
        Generate embeddings for multiple documents.

        This is a convenience method that returns just the embedding vectors.

        Args:
            documents: List of document texts

        Returns:
            List of embedding vectors
        """
        results = await self.embed_batch(documents)
        return [r.embedding for r in results]

    @classmethod
    def get_dimension(cls) -> int:
        """Get the embedding dimension for the current model."""
        return cls.EMBEDDING_DIMENSION


class EmbeddingServiceFactory:
    """Factory for creating embedding service instances."""

    _instance: EmbeddingService | None = None

    @classmethod
    def get_instance(cls, settings: Settings | None = None) -> EmbeddingService:
        """Get or create a singleton embedding service instance."""
        if cls._instance is None:
            cls._instance = EmbeddingService(settings)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton instance (useful for testing)."""
        cls._instance = None
