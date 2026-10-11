"""VOTEBOT-42: a client that reconnects with the same session id right behind a close must still get its answers.

The new socket is registered under the session id before the old connection's cleanup runs. That cleanup used to remove
whatever was registered under the id (the NEW socket), and `send_json` drops a message for an unknown id, so the answer went
nowhere. Nothing here uses a network: the endpoint is driven with scripted fake sockets, in the order that failed.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

from tests.unit.test_websocket_protocol import FakeAgent, _user_message
from votebot.api.routes import websocket as ws


class FakeSocket:
    """What the endpoint uses of a WebSocket: accept, send_json, receive_json, headers, client."""

    def __init__(self):
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.headers: dict = {}
        self.client = None

    async def accept(self):
        pass

    async def send_json(self, data):
        self.sent.append(data)

    async def receive_json(self):
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    def kinds(self) -> list[str]:
        return [frame["type"] for frame in self.sent]


async def frame(sock: FakeSocket, kind: str, timeout: float = 2.0):
    """Wait until `sock` has been sent a frame of this kind (a missing answer fails the test, not hangs it)."""
    async def wait():
        while kind not in sock.kinds():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), timeout)


@pytest.fixture
def logged(monkeypatch):
    """Every event the query logger is given, with nothing written anywhere."""
    events: list[dict] = []

    class Recorder:
        async def log_event(self, **kwargs):
            events.append(kwargs)

    FakeAgent.calls, FakeAgent.fail, FakeAgent.responder = [], False, None
    monkeypatch.setattr(ws, "VoteBotAgent", FakeAgent)
    monkeypatch.setattr(ws, "get_slack_service", lambda: SimpleNamespace(is_configured=False))
    monkeypatch.setattr(ws, "get_redis_store", lambda: SimpleNamespace(is_available=False))
    monkeypatch.setattr(ws, "manager", ws.ConnectionManager())
    ws.sessions.clear()
    import votebot.services.query_logger as query_logger

    monkeypatch.setattr(query_logger, "get_query_logger", lambda: Recorder())
    return events


def ended(events):
    return [e for e in events if e["event_type"] == "conversation_ended"]


async def first_conversation(sid="s1"):
    """A connection that asks one question and is still open. Returns (socket, endpoint task)."""
    sock = FakeSocket()
    task = asyncio.create_task(ws.websocket_chat_endpoint(sock, sid))
    await frame(sock, "session_info")
    sock.incoming.put_nowait(_user_message("first question"))
    await frame(sock, "stream_end")
    return sock, task


class TestManager:
    async def test_a_connection_removes_only_its_own_entry(self, monkeypatch):
        monkeypatch.setattr(ws, "get_slack_service", lambda: SimpleNamespace(is_configured=False))
        monkeypatch.setattr(ws, "get_redis_store", lambda: SimpleNamespace(is_available=False))
        manager = ws.ConnectionManager()
        old, new = FakeSocket(), FakeSocket()
        await manager.connect(old, "s1")
        await manager.connect(new, "s1")  # the reconnect registers first

        assert manager.disconnect("s1", old) is False  # the old one's cleanup: not its entry any more
        assert manager.active_connections["s1"] is new
        await manager.send_json("s1", {"type": "pong"})
        assert new.sent == [{"type": "pong"}]

        assert manager.disconnect("s1", new) is True
        assert "s1" not in manager.active_connections
        assert manager.disconnect("s1", new) is False  # nothing registered: not an error


class TestReconnectRace:
    async def test_the_answer_reaches_the_new_connection_when_the_old_ones_cleanup_runs_after_it_registered(self, logged):
        old, old_task = await first_conversation()

        new = FakeSocket()
        new_task = asyncio.create_task(ws.websocket_chat_endpoint(new, "s1"))  # reconnects behind the close
        await frame(new, "session_info")
        old.incoming.put_nowait(WebSocketDisconnect())  # ...and only now does the old connection's cleanup run
        await asyncio.wait_for(old_task, 2)

        assert ws.manager.active_connections["s1"] is new
        new.incoming.put_nowait(_user_message("second question"))
        await frame(new, "stream_end")  # used to time out: the new socket had been removed
        assert "stream_chunk" in new.kinds()

        new.incoming.put_nowait(WebSocketDisconnect())
        await asyncio.wait_for(new_task, 2)
        assert "s1" not in ws.manager.active_connections

    async def test_the_session_goes_on_when_the_old_connection_closes_late(self, logged):
        old, old_task = await first_conversation()
        new = FakeSocket()
        new_task = asyncio.create_task(ws.websocket_chat_endpoint(new, "s1"))
        await frame(new, "session_info")
        before = ws.sessions["s1"]["conversation_message_counter"]

        old.incoming.put_nowait(WebSocketDisconnect())
        await asyncio.wait_for(old_task, 2)
        await asyncio.sleep(0.02)  # let a fire-and-forget log task run, if one was started

        assert ended(logged) == []  # the conversation is not over: the visitor is still in it
        assert ws.sessions["s1"]["conversation_message_counter"] == before == 1  # not reset under the new connection

        new.incoming.put_nowait(WebSocketDisconnect())
        await asyncio.wait_for(new_task, 2)
        await asyncio.sleep(0.02)
        assert len(ended(logged)) == 1  # the conversation ends once, when the last connection closes

    async def test_an_old_connection_that_fails_late_does_not_remove_the_new_one_either(self, logged):
        old, old_task = await first_conversation()
        new = FakeSocket()
        new_task = asyncio.create_task(ws.websocket_chat_endpoint(new, "s1"))
        await frame(new, "session_info")

        old.incoming.put_nowait(RuntimeError("the old socket broke"))  # the other cleanup branch
        await asyncio.wait_for(old_task, 2)
        await asyncio.sleep(0.02)

        assert ws.manager.active_connections["s1"] is new
        assert ended(logged) == []
        new.incoming.put_nowait(_user_message("second question"))
        await frame(new, "stream_end")
        new.incoming.put_nowait(WebSocketDisconnect())
        await asyncio.wait_for(new_task, 2)


class TestOrdinaryClose:
    async def test_a_plain_close_still_ends_the_conversation_once_and_removes_the_connection(self, logged):
        sock, task = await first_conversation()
        sock.incoming.put_nowait(WebSocketDisconnect())
        await asyncio.wait_for(task, 2)
        await asyncio.sleep(0.02)

        assert "s1" not in ws.manager.active_connections
        events = ended(logged)
        assert len(events) == 1 and events[0]["terminal_state"] == "inactive_end"
        assert ws.sessions["s1"]["conversation_message_counter"] == 0  # reset, so a reconnect does not repeat the event

    async def test_a_failed_connection_is_cleaned_up_too(self, logged):
        sock, task = await first_conversation()
        sock.incoming.put_nowait(RuntimeError("boom"))
        await asyncio.wait_for(task, 2)
        await asyncio.sleep(0.02)

        assert "s1" not in ws.manager.active_connections
        events = ended(logged)
        assert len(events) == 1 and events[0]["terminal_state"] == "abandoned"
