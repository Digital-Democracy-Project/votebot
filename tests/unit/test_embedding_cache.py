"""VOTEBOT-46: a text is embedded once. Later calls reuse the vector; calls made while it is being fetched share it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from votebot.api.schemas.chat import PageContext
from votebot.config import Settings
from votebot.core.retrieval import RetrievalConfig, RetrievalService
from votebot.services.embeddings import EmbeddingService
from votebot.services.vector_store import VectorStoreService

DIM = 4


def _service(delay: float = 0.0) -> tuple[EmbeddingService, list[str]]:
    """A real EmbeddingService whose OpenAI client is a counting fake."""
    calls: list[str] = []

    async def create(model, input):
        calls.append(input)
        await asyncio.sleep(delay)
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=[float(len(input))] * DIM)], usage=SimpleNamespace(total_tokens=3), model=model
        )

    settings = Settings(openai_api_key="k", _env_file=None)
    service = EmbeddingService(settings)
    service.client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
    # no retry back-off in tests: the retry decorator would sleep seconds between attempts
    service.embed.retry.sleep = lambda s: asyncio.sleep(0)  # type: ignore[attr-defined]
    return service, calls


class TestEmbedQueryReuse:
    async def test_the_same_text_is_embedded_once(self):
        service, calls = _service()
        first = await service.embed_query("what does this bill do?")
        second = await service.embed_query("what does this bill do?")
        assert calls == ["what does this bill do?"]
        assert first == second == [23.0] * DIM

    async def test_different_texts_each_get_their_own_call(self):
        service, calls = _service()
        await service.embed_query("a")
        await service.embed_query("bb")
        await service.embed_query("a")
        assert sorted(calls) == ["a", "bb"]

    async def test_concurrent_identical_requests_make_one_call(self):
        service, calls = _service(delay=0.05)
        results = await asyncio.gather(*[service.embed_query("same text") for _ in range(5)])
        assert len(calls) == 1
        assert all(r == results[0] for r in results)

    async def test_a_failed_embedding_is_not_kept(self):
        service, calls = _service()
        working = service.client.embeddings.create

        async def down(model, input):
            raise RuntimeError("openai down")

        service.client.embeddings.create = down
        with pytest.raises(Exception):  # noqa: B017 -- tenacity's RetryError after its attempts
            await service.embed_query("text")
        assert "text" not in service._query_vectors  # nothing is kept for the failure
        service.client.embeddings.create = working
        assert await service.embed_query("text") == [4.0] * DIM  # the next call tries again and works

    async def test_the_oldest_entry_is_dropped_past_the_bound(self):
        service, calls = _service()
        service.QUERY_CACHE_MAX = 2
        for text in ("a", "bb", "ccc"):
            await service.embed_query(text)
        await service.embed_query("a")  # was evicted: embedded again
        await service.embed_query("ccc")  # still there
        assert calls == ["a", "bb", "ccc", "a"]

    async def test_a_caller_that_gives_up_does_not_cancel_the_request_others_share(self):
        service, calls = _service(delay=0.1)
        impatient = asyncio.ensure_future(asyncio.wait_for(service.embed_query("shared"), 0.01))
        patient = asyncio.ensure_future(service.embed_query("shared"))
        with pytest.raises(asyncio.TimeoutError):
            await impatient
        assert await patient == [6.0] * DIM
        assert len(calls) == 1


def _store_and_index(service: EmbeddingService) -> tuple[VectorStoreService, MagicMock]:
    index = MagicMock()
    index.query.return_value = SimpleNamespace(matches=[])
    pc = MagicMock()
    pc.list_indexes.return_value = [SimpleNamespace(name="ddp-knowledge-base")]
    pc.Index.return_value = index
    settings = Settings(pinecone_api_key="k", openai_api_key="k", pinecone_index_name="ddp-knowledge-base", _env_file=None)
    with patch("votebot.services.vector_store.Pinecone", return_value=pc), patch(
        "votebot.services.vector_store.EmbeddingService", return_value=service
    ):
        return VectorStoreService(settings), index


async def test_a_bill_page_message_embeds_each_distinct_text_once():
    """The real retrieval phases over a real vector store: several Pinecone queries, one embedding per distinct text."""
    service, calls = _service()
    store, index = _store_and_index(service)
    retrieval = RetrievalService.__new__(RetrievalService)
    retrieval.settings = store.settings
    retrieval.vector_store = store
    retrieval.bill_versions = SimpleNamespace(get_versions=lambda _id: asyncio.sleep(0, result=None))
    retrieval.config = RetrievalConfig(max_chunks=10, similarity_threshold=0.1, deduplicate=False)
    page = PageContext(type="bill", id="HB 1", jurisdiction="FL", session="2026", ocd_bill_id="a3f7c0d1-1111-4222-8333-444455556666")

    await retrieval.retrieve(query="Who supports this bill and how did the vote go?", page_context=page)

    assert index.query.call_count >= 3  # the phases ran
    assert len(calls) == len(set(calls))  # no text was embedded twice
    assert len(calls) < index.query.call_count  # which is the saving
