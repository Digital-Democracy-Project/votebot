"""VOTEBOT-49: the first chat after a start does not pay for the Pinecone index lookup or the first connections.

Nothing here reaches Pinecone or OpenAI: both clients are fakes. The warm-up only reads: a missing index is reported,
never created.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog.testing

from votebot import main as main_module
from votebot.config import Settings
from votebot.core import agent as agent_module
from votebot.services.vector_store import VectorStoreService


def _store(index_exists: bool = True, stats_fail: bool = False) -> tuple[VectorStoreService, MagicMock, MagicMock]:
    index = MagicMock()
    if stats_fail:
        index.describe_index_stats.side_effect = RuntimeError("pinecone down")
    pc = MagicMock()
    pc.list_indexes.return_value = [SimpleNamespace(name="ddp-knowledge-base")] if index_exists else []
    pc.Index.return_value = index
    settings = Settings(pinecone_api_key="k", openai_api_key="k", pinecone_index_name="ddp-knowledge-base", _env_file=None)
    with patch("votebot.services.vector_store.Pinecone", return_value=pc), patch("votebot.services.vector_store.EmbeddingService"):
        return VectorStoreService(settings), pc, index


class TestTheVectorStoreWarm:
    async def test_it_opens_the_index_and_touches_it(self):
        store, pc, index = _store()
        assert await store.warm() is True
        index.describe_index_stats.assert_called_once()
        pc.create_index.assert_not_called()

    async def test_the_first_chat_then_finds_the_index_open(self):
        store, pc, index = _store()
        await store.warm()
        lookups = pc.list_indexes.call_count
        assert store.index is index  # what a query uses
        assert pc.list_indexes.call_count == lookups  # no index lookup left for the first chat

    async def test_a_missing_index_is_reported_and_never_created(self):
        store, pc, _index = _store(index_exists=False)
        assert await store.warm() is False
        pc.create_index.assert_not_called()
        assert store._index is None

    async def test_it_runs_off_the_event_loop(self):
        store, pc, _index = _store()
        threads = []
        pc.list_indexes.side_effect = lambda: threads.append(__import__("threading").get_ident()) or [SimpleNamespace(name="ddp-knowledge-base")]
        await store.warm()
        assert threads and threads[0] != __import__("threading").get_ident()


def _services(store, embed):
    return SimpleNamespace(retrieval=SimpleNamespace(vector_store=SimpleNamespace(
        warm=store, embedding_service=SimpleNamespace(embed=embed))))


class TestWarmUpServices:
    async def test_it_warms_the_shared_services_chat_uses(self):
        warm, embed = AsyncMock(return_value=True), AsyncMock()
        with patch.object(agent_module, "_services_for", return_value=_services(warm, embed)) as services:
            await agent_module.warm_up_services(Settings(_env_file=None))
        services.assert_called_once()
        warm.assert_awaited_once()
        embed.assert_awaited_once_with("warm up")

    async def test_it_says_it_finished(self):
        services = _services(AsyncMock(return_value=True), AsyncMock())
        with patch.object(agent_module, "_services_for", return_value=services), structlog.testing.capture_logs() as logs:
            await agent_module.warm_up_services(Settings(_env_file=None))
        done = [e for e in logs if e["event"] == "Warm-up finished"]
        assert done and done[0]["failed"] == [] and done[0]["pinecone_index_found"] is True

    @pytest.mark.parametrize("broken", ["pinecone", "embeddings", "both", "missing_index", "services"])
    async def test_whatever_is_unreachable_it_does_not_raise_and_says_so(self, broken):
        warm = AsyncMock(side_effect=RuntimeError("pinecone down")) if broken in ("pinecone", "both") else AsyncMock(return_value=broken != "missing_index")
        embed = AsyncMock(side_effect=RuntimeError("openai down")) if broken in ("embeddings", "both") else AsyncMock()
        patcher = patch.object(agent_module, "_services_for", side_effect=RuntimeError("no keys")) if broken == "services" \
            else patch.object(agent_module, "_services_for", return_value=_services(warm, embed))
        with patcher, structlog.testing.capture_logs() as logs:
            await agent_module.warm_up_services(Settings(_env_file=None))
        assert any(e["event"].startswith("Warm-up") and e["log_level"] == "warning" or e["event"] == "Warm-up incomplete" for e in logs)
        assert not any(e["event"] == "Warm-up finished" for e in logs)

    async def test_the_embedding_call_does_not_leave_an_entry_in_the_query_cache(self):
        # it uses `embed`, the uncached call: a throwaway text must not take a place a visitor's question could use
        calls = []
        embed = AsyncMock(side_effect=lambda text: calls.append(text))
        with patch.object(agent_module, "_services_for", return_value=_services(AsyncMock(return_value=True), embed)):
            await agent_module.warm_up_services(Settings(_env_file=None))
        assert calls == ["warm up"]


@pytest.fixture
def no_redis():
    """The lifespan's Redis, button-cache and OpenStates-settings parts are not under test (a real Redis on a
    developer's machine makes the subscriber's shutdown slow): stand-ins for them."""
    store = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock())
    # nor is the OpenStates settings check: it would make these tests depend on the machine's .env
    with patch("votebot.main.check_openstates_config"), patch(
        "votebot.services.redis_store.get_redis_store", return_value=store
    ), patch(
        "votebot.services.button_cache.reconcile_on_startup", AsyncMock()
    ), patch("votebot.services.button_cache.start_invalidate_subscriber", AsyncMock()), patch(
        "votebot.services.button_cache.stop_invalidate_subscriber", AsyncMock()
    ):
        yield


@pytest.mark.usefixtures("no_redis")
class TestStartupIsNeverBlockedOrStopped:
    async def test_startup_does_not_wait_for_a_slow_warm_up(self):
        started = asyncio.Event()

        async def slow(settings):
            started.set()
            await asyncio.sleep(30)

        app = SimpleNamespace()
        with patch.object(agent_module, "warm_up_services", slow):
            began = asyncio.get_running_loop().time()
            async with main_module.lifespan(app):
                assert asyncio.get_running_loop().time() - began < 5  # not 30 s
                await asyncio.wait_for(started.wait(), 1)  # and it did start

    async def test_a_warm_up_still_running_is_cancelled_and_has_finished_cancelling_when_the_app_stops(self):
        cleanup = []

        async def blocked(settings):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await asyncio.sleep(0.02)  # cleanup that takes a moment
                cleanup.append("done")
                raise

        with patch.object(agent_module, "warm_up_services", blocked):
            async with main_module.lifespan(SimpleNamespace()):
                await asyncio.sleep(0.01)
        assert cleanup == ["done"]  # finished before the lifespan returned, not left to the loop
        assert not (asyncio.all_tasks() - {asyncio.current_task()})

    async def test_the_warm_up_is_cleaned_up_when_the_app_stops_on_an_error_too(self):
        cleanup = []

        async def blocked(settings):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cleanup.append("done")
                raise

        with patch.object(agent_module, "warm_up_services", blocked), pytest.raises(RuntimeError, match="body failed"):
            async with main_module.lifespan(SimpleNamespace()):
                await asyncio.sleep(0.01)
                raise RuntimeError("body failed")
        assert cleanup == ["done"]
        assert not (asyncio.all_tasks() - {asyncio.current_task()})

    async def test_a_warm_up_that_ended_with_an_exception_leaves_nothing_unretrieved(self):
        reported = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: reported.append(ctx))

        async def broken(settings):
            raise RuntimeError("everything is down")

        with patch.object(agent_module, "warm_up_services", broken):
            async with main_module.lifespan(SimpleNamespace()):
                await asyncio.sleep(0.01)
        import gc

        gc.collect()
        await asyncio.sleep(0)
        assert reported == []  # no "Task exception was never retrieved"

    async def test_startup_succeeds_when_the_warm_up_blows_up(self):
        async def broken(settings):
            raise RuntimeError("everything is down")

        app = SimpleNamespace()
        with patch.object(agent_module, "warm_up_services", broken):
            async with main_module.lifespan(app):
                await asyncio.sleep(0.01)  # the failure happens in the background task, not in startup
