"""UI event channel: /ws/ui auth + wiring, UiBroker frame fan-out.

The extracted frontend (Discord bot) subscribes here; every event the
in-process bot used to get through the notifier callback slot must arrive
as one JSON frame."""
import asyncio

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.ui_events import UiBroker

class FakeWs:
    def __init__(self):
        self.frames = []
        self.dead = False
        self._forever = asyncio.Event()

    async def receive(self):
        await self._forever.wait()  # holds attach() open until released
        return {"type": "websocket.disconnect"}

    async def send_json(self, frame):
        if self.dead:
            raise RuntimeError("connection gone")
        self.frames.append(frame)


def make_client() -> TestClient:
    return TestClient(create_app(Settings(admin_token="t",
                                          sqlite_path=":memory:")))


@pytest.fixture
async def hub():
    app = create_app(Settings(admin_token="t", sqlite_path=":memory:"))
    async with app.router.lifespan_context(app):
        yield app


def test_ui_ws_rejects_missing_token():
    with make_client() as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/ui"):
                pass
        assert exc_info.value.code == 1008


def _wait_attached(client, count: int) -> None:
    """Block until the app's broker holds `count` clients (attach() runs
    on the app's loop, a beat after the client sees the accept)."""
    broker = client.app.state.ui_broker

    async def poll():
        for _ in range(200):
            if len(broker._clients) == count:
                return True
            await asyncio.sleep(0.01)
        return False

    assert client.portal.call(poll), f"broker never reached {count} clients"


def test_ui_ws_admin_stays_open_and_receives_broadcast():
    """The channel is push-only: an admin connection must stay open after
    the handshake and get every frame the broker fans out."""
    with make_client() as client:
        broker = client.app.state.ui_broker
        with client.websocket_connect(
            "/ws/ui", headers={"authorization": "Bearer t"},
        ) as ws:
            _wait_attached(client, 1)
            client.portal.call(broker.on_message, "sess_x", "hi", 1)
            assert ws.receive_json() == {
                "type": "session.message", "session_id": "sess_x",
                "text": "hi", "message_id": 1}


def test_ui_ws_client_frames_do_not_detach():
    """Whatever a client sends (text or binary) is ignored; it stays
    attached and keeps receiving."""
    with make_client() as client:
        broker = client.app.state.ui_broker
        with client.websocket_connect(
            "/ws/ui", headers={"authorization": "Bearer t"},
        ) as ws:
            _wait_attached(client, 1)
            ws.send_text("ping")
            ws.send_bytes(b"\x00\x01")
            client.portal.call(broker.on_completed, "sess_x")
            assert ws.receive_json() == {
                "type": "session.completed", "session_id": "sess_x"}
            _wait_attached(client, 1)


def test_ui_ws_disconnect_releases_client():
    with make_client() as client:
        broker = client.app.state.ui_broker
        with client.websocket_connect(
            "/ws/ui", headers={"authorization": "Bearer t"},
        ):
            _wait_attached(client, 1)
        _wait_attached(client, 0)
        client.portal.call(broker.on_completed, "sess_x")  # must not raise


def test_ui_ws_rejects_agent_pinned_token():
    """A per-agent token is valid for REST but /ws/ui is admin-only: the
    broker fans every session's frames to every socket, so attaching with
    a pinned token would bypass the per-session ACL."""
    with make_client() as client:
        _, secret = client.portal.call(
            client.app.state.tokens.issue, "agent-x")
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/ws/ui", headers={"authorization": f"Bearer {secret}"},
            ):
                pass
        assert exc_info.value.code == 1008


def test_lifespan_wires_broker_into_hub(hub):
    broker = hub.state.ui_broker
    assert isinstance(broker, UiBroker)
    assert hub.state.sessions.notifier is broker
    assert hub.state.sync.on_applied == broker.on_sync_applied


async def _attached(broker, *sockets):
    tasks = [asyncio.create_task(broker.attach(w)) for w in sockets]
    await asyncio.sleep(0)  # let attach() register its clients
    return tasks


async def test_broker_broadcasts_notifier_events():
    broker = UiBroker()
    a, b = FakeWs(), FakeWs()
    tasks = await _attached(broker, a, b)
    await broker.on_message("s1", "hello", 7)
    await broker.on_delta("s1", "wor")
    await broker.on_activity("s1", "bash: ls")
    await broker.on_completed("s1")
    await broker.on_failed("s1", "boom")
    await broker.on_agent_disconnected("a1")
    await broker.on_sync_applied("a1", ["s1", "s2"])
    expected = [
        {"type": "session.message", "session_id": "s1", "text": "hello",
         "message_id": 7},
        {"type": "session.delta", "session_id": "s1", "draft": "wor"},
        {"type": "session.activity", "session_id": "s1", "label": "bash: ls"},
        {"type": "session.completed", "session_id": "s1"},
        {"type": "session.failed", "session_id": "s1", "reason": "boom"},
        {"type": "agent.disconnected", "agent_id": "a1"},
        {"type": "sync.applied", "agent_id": "a1", "session_ids": ["s1", "s2"]},
    ]
    for w in (a, b):
        assert w.frames == expected
    for t in tasks:
        t.cancel()


async def test_broker_drops_dead_clients():
    broker = UiBroker()
    dead, alive = FakeWs(), FakeWs()
    dead.dead = True
    tasks = await _attached(broker, dead, alive)
    await broker.on_completed("s1")  # dead client must not break the fan-out
    assert alive.frames == [{"type": "session.completed", "session_id": "s1"}]
    for t in tasks:
        t.cancel()
