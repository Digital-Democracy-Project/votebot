"""VOTEBOT-36: the Pinecone client is synchronous, so a query must not run on the event loop.

Called on the loop, one slow request stalls every chat on the worker and the health checks with it. Nothing here
touches Pinecone or OpenAI: both clients are fakes.
"""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from votebot.config import Settings
from votebot.services.vector_store import VectorStoreService


class FakeIndex:
    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.threads: list[int] = []

    def query(self, **kwargs):
        self.threads.append(threading.get_ident())
        time.sleep(self.delay)  # a blocking call, like the real client
        return SimpleNamespace(matches=[SimpleNamespace(id="a", score=0.9, metadata={"content": "text", "k": "v"})])

    def describe_index_stats(self):
        self.threads.append(threading.get_ident())
        return SimpleNamespace(total_vector_count=1)


def make_store(index: FakeIndex) -> tuple[VectorStoreService, MagicMock]:
    settings = Settings(_env_file=None, pinecone_api_key="k", openai_api_key="k", pinecone_index_name="ddp-knowledge-base")
    pc = MagicMock()
    pc.list_indexes.return_value = [SimpleNamespace(name="ddp-knowledge-base")]
    pc.Index.return_value = index
    with patch("votebot.services.vector_store.Pinecone", return_value=pc), patch(
        "votebot.services.vector_store.EmbeddingService"
    ) as embeddings:
        store = VectorStoreService(settings)
    store.embedding_service = SimpleNamespace(embed_query=_embed)
    del embeddings
    return store, pc


async def _embed(text: str) -> list[float]:
    return [0.0]


async def test_a_query_runs_off_the_event_loop_thread():
    index = FakeIndex()
    store, _ = make_store(index)
    results = await store.query("q", top_k=1)
    assert index.threads and index.threads[0] != threading.get_ident()
    assert [(r.id, r.content, r.metadata) for r in results] == [("a", "text", {"k": "v"})]


async def test_the_loop_keeps_running_while_a_slow_query_is_in_flight():
    store, _ = make_store(FakeIndex(delay=0.4))
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    task = asyncio.create_task(ticker())
    await store.query("q")
    task.cancel()
    assert ticks >= 10  # on the loop the 0.4 s call would have allowed none


async def test_concurrent_first_queries_look_the_index_up_once():
    store, pc = make_store(FakeIndex(delay=0.05))
    await asyncio.gather(*(store.query("q") for _ in range(8)))
    assert pc.list_indexes.call_count == 1
    assert pc.Index.call_count == 1


async def test_the_health_check_runs_off_the_event_loop_thread():
    index = FakeIndex()
    store, _ = make_store(index)
    assert await store.health_check() is True
    assert index.threads[0] != threading.get_ident()
