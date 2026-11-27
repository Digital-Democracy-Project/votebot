"""VOTEBOT-78: `/health/ready` runs one real, tiny Pinecone query, so a refused read shows as unhealthy.

On 2026-11-26 Pinecone refused every query (the monthly read limit, VOTEBOT-77) while the old check, an index-statistics
call, kept answering: the chat said "Online" and failed every message. The check is now the smallest real query there is
(one match, no metadata, no values), at most one a window, and it does not call OpenAI. Nothing here touches Pinecone or
OpenAI: the clients are fakes.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from votebot.api.routes import health
from votebot.config import Settings
from votebot.services.embeddings import EmbeddingService
from votebot.services.llm import HEALTH_FAIL_TTL, HEALTH_OK_TTL
from votebot.services.vector_store import VectorStoreService


class RateLimited(Exception):
    """Stands in for Pinecone's 429 (the real class lives in the pinecone package)."""


class FakeIndex:
    """`describe_index_stats` always answers, as it did during the outage; only `query` can fail."""

    def __init__(self, query_error: Exception | list | None = None, delay: float = 0.0):
        self.query_calls: list[dict] = []
        self.stats_calls = 0
        self._error = query_error
        self._delay = delay

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        if self._delay:
            import time

            time.sleep(self._delay)
        error = self._error.pop(0) if isinstance(self._error, list) else self._error
        if error is not None:
            raise error
        return SimpleNamespace(matches=[])

    def describe_index_stats(self):
        self.stats_calls += 1
        return SimpleNamespace(total_vector_count=1, dimension=3072)


def make_store(index: FakeIndex) -> VectorStoreService:
    settings = Settings(
        _env_file=None,
        pinecone_api_key="k",
        openai_api_key="k",
        pinecone_index_name="ddp-knowledge-base",
        pinecone_namespace="default",
    )
    pc = MagicMock()
    pc.list_indexes.return_value = [SimpleNamespace(name="ddp-knowledge-base")]
    pc.Index.return_value = index
    with patch("votebot.services.vector_store.Pinecone", return_value=pc), patch("votebot.services.vector_store.EmbeddingService"):
        store = VectorStoreService(settings)
    store.embedding_service = SimpleNamespace(embed_query=AsyncMock(side_effect=AssertionError("the check must not embed")))
    return store


def age(store: VectorStoreService, seconds: float) -> None:
    when, outcome = store._health
    store._health = (when - seconds, outcome)


class TestTheCheckIsOneTinyRealQuery:
    async def test_it_asks_for_one_match_without_metadata_or_values(self):
        index = FakeIndex()
        assert await make_store(index).health_check() is True
        assert len(index.query_calls) == 1
        call = index.query_calls[0]
        assert call["top_k"] == 1
        assert call["include_metadata"] is False and call["include_values"] is False
        assert call["namespace"] == "default"

    async def test_the_probe_is_a_nonzero_vector_of_the_embedding_dimension_and_nothing_is_embedded(self):
        index = FakeIndex()
        store = make_store(index)
        await store.health_check()
        vector = index.query_calls[0]["vector"]
        assert len(vector) == EmbeddingService.get_dimension()
        assert any(v != 0 for v in vector)  # an all-zero vector is refused by a cosine index
        store.embedding_service.embed_query.assert_not_awaited()

    async def test_the_statistics_call_is_not_what_decides_health(self):
        # During the outage the statistics call answered and the query was refused: the check must follow the query.
        index = FakeIndex(query_error=RateLimited("429 egress limit"))
        with pytest.raises(RateLimited):
            await make_store(index).health_check()

    async def test_the_query_runs_off_the_event_loop_thread(self):
        import threading

        seen: list[int] = []

        class Recording(FakeIndex):
            def query(self, **kwargs):
                seen.append(threading.get_ident())
                return super().query(**kwargs)

        await make_store(Recording()).health_check()
        assert seen and seen[0] != threading.get_ident()


class TestOneRequestAWindow:
    async def test_repeated_checks_inside_the_window_make_one_query(self):
        index = FakeIndex()
        store = make_store(index)
        for _ in range(20):
            assert await store.health_check() is True
        assert len(index.query_calls) == 1

    async def test_after_the_window_the_next_check_queries_again(self):
        index = FakeIndex()
        store = make_store(index)
        await store.health_check()
        age(store, HEALTH_OK_TTL + 1)
        await store.health_check()
        assert len(index.query_calls) == 2

    async def test_checks_that_arrive_together_share_one_query(self):
        index = FakeIndex(delay=0.05)
        store = make_store(index)
        assert await asyncio.gather(*[store.health_check() for _ in range(10)]) == [True] * 10
        assert len(index.query_calls) == 1


class TestItFailsWhenPineconeRefusesTheRead:
    @pytest.mark.parametrize(
        "error",
        [RateLimited("429 egress limit"), ConnectionError("unreachable"), TimeoutError("slow"), RuntimeError("dimension mismatch")],
    )
    async def test_any_failure_raises(self, error):
        with pytest.raises(type(error)):
            await make_store(FakeIndex(query_error=error)).health_check()

    async def test_a_failure_is_remembered_briefly_not_for_a_minute(self):
        index = FakeIndex(query_error=RateLimited("429"))
        store = make_store(index)
        for _ in range(5):
            with pytest.raises(RateLimited):
                await store.health_check()
        assert len(index.query_calls) == 1  # a poll storm during an outage does not query each time
        age(store, HEALTH_FAIL_TTL + 1)
        with pytest.raises(RateLimited):
            await store.health_check()
        assert len(index.query_calls) == 2  # but it looks again soon

    async def test_a_recovery_is_seen_after_the_short_window(self):
        store = make_store(FakeIndex(query_error=[RateLimited("429"), None]))
        with pytest.raises(RateLimited):
            await store.health_check()
        age(store, HEALTH_FAIL_TTL + 1)
        assert await store.health_check() is True

    async def test_a_healthy_result_does_not_hide_a_later_failure_forever(self):
        store = make_store(FakeIndex(query_error=[None, RateLimited("429")]))
        assert await store.health_check() is True
        age(store, HEALTH_OK_TTL + 1)
        with pytest.raises(RateLimited):
            await store.health_check()


class TestReadinessUsesTheSharedService:
    async def _readiness(self, store: VectorStoreService):
        shared = SimpleNamespace(
            retrieval=SimpleNamespace(vector_store=store), llm=SimpleNamespace(health_check=AsyncMock(return_value=True))
        )
        with patch("votebot.core.agent._services_for", return_value=shared), patch.object(
            health, "openstates_base_url", return_value="http://api-v3.test"
        ), patch.object(health.httpx, "AsyncClient") as client:
            client.return_value.__aenter__.return_value.get = AsyncMock(return_value=SimpleNamespace(raise_for_status=lambda: None))
            return await health.readiness_check(Settings(openai_api_key="k", _env_file=None))

    async def test_a_refused_query_marks_pinecone_unhealthy_and_the_service_degraded(self):
        result = await self._readiness(make_store(FakeIndex(query_error=RateLimited("429 egress limit"))))
        assert result.dependencies["pinecone"] == "unhealthy"
        assert result.status == "degraded"

    async def test_polling_readiness_makes_one_query(self):
        index = FakeIndex()
        store = make_store(index)
        for _ in range(10):
            result = await self._readiness(store)
        assert result.dependencies["pinecone"] == "healthy" and len(index.query_calls) == 1

    def test_the_windows_are_the_model_checks(self):
        assert HEALTH_OK_TTL == 60.0 and HEALTH_FAIL_TTL == 10.0
