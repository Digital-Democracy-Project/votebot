"""VOTEBOT-38: with several workers a reconnecting visitor lands on whichever one accepts the connection.

A session lived only in the memory of the worker that served it, so a visitor who came back to another worker lost the
history, and a Slack handoff in progress (visitor messages must go to the thread, not the bot). The session is now kept
in Redis too. "Another worker" is simulated by emptying this process's sessions and building a new ConnectionManager.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from tests.unit.test_websocket_protocol import FakeAgent, _read_until, _user_message
from votebot.api.routes import websocket as ws


class FakeRedis:
    """What RedisStore does for sessions: JSON in, JSON out (so what is saved must be JSON-serialisable)."""

    is_available = True

    def __init__(self):
        self.data: dict[str, str] = {}

    async def save_session(self, session_id, session):
        self.data[session_id] = json.dumps(session, default=str)

    async def load_session(self, session_id):
        return json.loads(self.data[session_id]) if session_id in self.data else None

    async def set_thread_mapping(self, *a):
        pass

    async def get_session_for_thread(self, *a):
        return None

    async def subscribe_agent_events(self, *a):
        pass


@pytest.fixture
def redis(monkeypatch):
    return FakeRedis()


class FakeSlack:
    is_configured = False

    def __init__(self):
        self.relayed: list[tuple[str, str]] = []

    async def relay_user_message(self, thread_ts, message):
        self.relayed.append((thread_ts, message))
        return True


@pytest.fixture
def slack():
    return FakeSlack()


@pytest.fixture
def make_worker(monkeypatch, redis, slack):
    """A function that turns this process into a freshly started worker, and returns a client for it."""
    FakeAgent.calls, FakeAgent.fail, FakeAgent.responder = [], False, None
    monkeypatch.setattr(ws, "VoteBotAgent", FakeAgent)
    monkeypatch.setattr(ws, "get_slack_service", lambda: slack)
    monkeypatch.setattr(ws, "get_redis_store", lambda: redis)
    import votebot.services.query_logger as query_logger

    monkeypatch.setattr(query_logger, "get_query_logger", lambda: None)

    def worker() -> TestClient:
        ws.sessions.clear()  # a different process: it remembers nothing
        monkeypatch.setattr(ws, "manager", ws.ConnectionManager())
        app = FastAPI()
        app.include_router(ws.router)
        return TestClient(app)

    return worker


def test_history_follows_the_visitor_to_another_worker(make_worker):
    with make_worker().websocket_connect("/ws/chat?session_id=s1") as conn:
        conn.receive_json()
        conn.send_json(_user_message("hello"))
        _read_until(conn, "stream_end")

    with make_worker().websocket_connect("/ws/chat?session_id=s1") as conn:  # worker 2
        assert conn.receive_json()["payload"]["restored"] is True  # asserted before the next frame is awaited: no hang if not
        restored = conn.receive_json()
        assert [(m["role"], m["content"]) for m in restored["payload"]["messages"]] == [
            ("user", "hello"),
            ("assistant", "The bill does X."),
        ]
        conn.send_json(_user_message("and then?"))
        _read_until(conn, "stream_end")
    assert [m["content"] for m in FakeAgent.calls[-1]["conversation_history"]] == ["hello", "The bill does X."]


def test_a_handoff_in_progress_survives_the_move(make_worker, redis, slack):
    with make_worker().websocket_connect("/ws/chat?session_id=s1") as conn:
        conn.receive_json()
        conn.send_json(_user_message("I want a human"))
        _read_until(conn, "stream_end")
        ws.sessions["s1"].update(handoff_active=True, slack_thread_ts="111.222")
        conn.send_json({"type": "context_update", "payload": {"page_context": {"type": "general"}}})  # any save point
        conn.send_json({"type": "ping"})
        assert conn.receive_json()["type"] == "pong"

    answers_before = len(FakeAgent.calls)
    with make_worker().websocket_connect("/ws/chat?session_id=s1") as conn:  # worker 2
        assert conn.receive_json()["payload"]["restored"] is True
        conn.receive_json()
        assert ws.sessions["s1"]["handoff_active"] is True and ws.sessions["s1"]["slack_thread_ts"] == "111.222"
        conn.send_json(_user_message("are you there?"))  # relayed to Slack, not answered by the bot
        conn.send_json({"type": "ping"})
        assert conn.receive_json()["type"] == "pong"
    assert len(FakeAgent.calls) == answers_before
    assert slack.relayed == [("111.222", "are you there?")]


def test_a_worker_that_missed_the_move_cannot_overwrite_the_newer_session(make_worker, redis):
    old = make_worker()  # worker 1: the visitor talks to it, then moves away without it noticing
    with old.websocket_connect("/ws/chat?session_id=s1") as conn:
        conn.receive_json()
        conn.send_json(_user_message("first"))
        _read_until(conn, "stream_end")
        stale = json.loads(json.dumps(ws.sessions["s1"]))

    with make_worker().websocket_connect("/ws/chat?session_id=s1") as conn:  # worker 2 hears the second message
        assert conn.receive_json()["payload"]["restored"] is True
        conn.receive_json()
        conn.send_json(_user_message("second"))
        _read_until(conn, "stream_end")
    newer = json.loads(redis.data["s1"])
    assert len(newer["messages"]) == 4

    ws.sessions.clear()
    ws.sessions["s1"] = stale  # worker 1 finally notices its dead socket and saves what it remembers
    import asyncio

    asyncio.run(ws.ConnectionManager().save_session("s1"))
    assert json.loads(redis.data["s1"]) == newer


async def test_a_slack_reply_delivered_on_this_worker_is_saved(monkeypatch, redis):
    monkeypatch.setattr(ws, "get_redis_store", lambda: redis)
    ws.sessions.clear()
    manager = ws.ConnectionManager()
    sent = []
    manager.active_connections["s1"] = SimpleNamespace(send_json=lambda data: _record(sent, data))
    ws.sessions["s1"] = {"messages": [], "handoff_active": True, "last_message_time": 1.0}

    await manager._handle_redis_event(
        {"event_type": "agent_message", "session_id": "s1", "payload": {"text": "Hi, a human here", "agent_name": "Sam"}}
    )
    saved = json.loads(redis.data["s1"])
    assert [m["content"] for m in saved["messages"]] == ["Hi, a human here"]
    assert [frame["type"] for frame in sent] == ["agent_joined", "agent_message"]


async def _record(sent, data):
    sent.append(data)


class FakeRedisClient:
    def __init__(self, fail=False):
        self.kv, self.ttls, self.fail = {}, {}, fail

    async def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("down")
        self.kv[key], self.ttls[key] = value, ex

    async def get(self, key):
        if self.fail:
            raise ConnectionError("down")
        return self.kv.get(key)


async def test_redis_store_round_trips_a_session_with_a_ttl_and_never_raises():
    from votebot.services.redis_store import SESSION_PREFIX, SESSION_TTL, RedisStore

    store = RedisStore()
    assert await store.load_session("s1") is None  # no Redis: nothing, no error
    await store.save_session("s1", {"messages": []})

    store._client = FakeRedisClient()
    await store.save_session("s1", {"messages": [{"role": "user", "content": "hi"}], "last_message_time": 5.0})
    assert store._client.ttls[f"{SESSION_PREFIX}s1"] == SESSION_TTL
    assert (await store.load_session("s1"))["messages"][0]["content"] == "hi"
    assert await store.load_session("other") is None

    store._client = FakeRedisClient(fail=True)  # Redis failing costs the save, not the chat
    await store.save_session("s1", {})
    assert await store.load_session("s1") is None
