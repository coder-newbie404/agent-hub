from sqlalchemy import select

import json
import time

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Base, MessageRow
from app.ws import ws_agent


def make_client(admin_token: str = "secret") -> TestClient:
    from app.config import Settings
    # lifespan runs ensure_bootstrap: the admin token becomes a wildcard
    # bootstrap WS token, so tests can register with "Bearer <token>".
    app = create_app(Settings(admin_token=admin_token, sqlite_path=":memory:"))
    return TestClient(app)


def test_bad_token_rejected():
    client = make_client("secret")
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/agent"):  # no auth header
            pass
    assert exc_info.value.code == 1008


def test_register_then_heartbeat_then_disconnect():
    client = make_client("secret")
    with client:  # lifespan: schema bootstrap + sweep loop
        with client.websocket_connect(
            "/ws/agent", headers={"authorization": "Bearer secret"},
        ) as ws:
            ws.send_json({"protocol_version": 1, "type": "agent.register",
                          "payload": {"agent_id": "omp-local", "name": "OMP Local",
                                      "capabilities": ["chat"]}})
            # register is consumed silently; heartbeat must keep the agent online
            ws.send_json({"protocol_version": 1, "type": "agent.heartbeat", "payload": {}})
            reg = client.app.state.registry
            deadline = 5.0
            while not reg.online("omp-local") and deadline > 0:
                time.sleep(0.05); deadline -= 0.05
            assert reg.online("omp-local")
            ws.close()  # client-initiated close → server processes the disconnect
            time.sleep(0.2)
            assert not client.app.state.registry.online("omp-local")


async def test_malformed_frame_marks_agent_offline():
    from app.config import Settings

    app = create_app(Settings(admin_token="test-token", sqlite_path=":memory:"))
    from app.tokens import TokenService
    registry = app.state.registry
    maker = app.state.sessions._maker
    async with maker.kw["bind"].begin() as conn:  # schema on the app's own engine
        await conn.run_sync(Base.metadata.create_all)
    await app.state.tokens.ensure_bootstrap("test-token")

    class _RawFrameSocket(_AgentSocket):
        """Like _AgentSocket, but a str frame makes receive_json raise, as
        starlette's would for a malformed text frame."""

        async def receive_json(self):
            frame = self._frames.pop(0) if self._frames else None
            if frame is None:
                raise WebSocketDisconnect(code=1000)
            if isinstance(frame, str):
                raise json.JSONDecodeError("Expecting value", frame, 0)
            return frame

    sock = _RawFrameSocket(app, [
        {"protocol_version": 1, "type": "agent.register",
         "payload": {"agent_id": "omp-flaky", "name": "Flaky", "capabilities": []}},
        '{"type":',  # malformed JSON mid-session kills the loop
    ])
    with pytest.raises(json.JSONDecodeError):
        await ws_agent(sock)
    assert not registry.online("omp-flaky")


class _AgentSocket:
    """Minimal WebSocket stand-in: drives ws_agent directly on the test loop
    (the TestClient portal runs its own loop, which would split the app's
    in-memory SQLite database in two)."""

    def __init__(self, app, frames):
        self.app = app
        self.headers = {"authorization": "Bearer test-token"}
        self.closed_codes: list[int] = []
        self._frames = list(frames)

    async def accept(self):
        pass

    async def close(self, code: int = 1000):
        self.closed_codes.append(code)

    async def receive_json(self):
        if self._frames:
            return self._frames.pop(0)
        raise WebSocketDisconnect(code=1000)


async def test_dispatch_routes_agent_frames_to_session_manager():
    from app.config import Settings

    app = create_app(Settings(admin_token="test-token", sqlite_path=":memory:"))
    from app.tokens import TokenService
    sessions = app.state.sessions
    maker = sessions._maker
    async with maker.kw["bind"].begin() as conn:  # schema on the app's own engine
        await conn.run_sync(Base.metadata.create_all)
    await app.state.tokens.ensure_bootstrap("test-token")
    s1 = await sessions.create("omp-local", "owner1", None)
    s2 = await sessions.create("omp-local", "owner1", None)
    frames = [
        {"protocol_version": 1, "type": "agent.register",
         "payload": {"agent_id": "omp-local", "name": "local", "capabilities": []}},
        {"protocol_version": 1, "type": "session.waiting", "session_id": s1.id,
         "payload": {"state": "running"}},
        {"protocol_version": 1, "type": "agent.message", "session_id": s1.id,
         "payload": {"text": "hi human"}},
        {"protocol_version": 1, "type": "session.completed", "session_id": s1.id,
         "payload": {"omp_session_id": "omp-9"}},
        {"protocol_version": 1, "type": "session.failed", "session_id": s1.id,
         "payload": {"reason": "cancelled"}},
        {"protocol_version": 1, "type": "session.waiting", "session_id": s2.id,
         "payload": {"state": "running"}},
        {"protocol_version": 1, "type": "session.delta", "session_id": s2.id,
         "payload": {"reset": True}},
        {"protocol_version": 1, "type": "session.delta", "session_id": s2.id,
         "payload": {"text": "draft "}},
        {"protocol_version": 1, "type": "session.activity", "session_id": s2.id,
         "payload": {"kind": "tool", "state": "start", "tool": "bash",
                     "detail": "echo"}},
    ]
    await ws_agent(_AgentSocket(app, frames))

    done = await sessions.get(s1.id)
    assert done.status == "cancelled"  # completed→cancelled via reason routing
    assert done.omp_session_id == "omp-9"
    assert (await sessions.get(s2.id)).status == "running"  # waiting("running")
    assert sessions._drafts[s2.id] == "draft "
    assert sessions.activity[s2.id] == "bash: echo"
    async with maker() as s:
        msgs = (await s.execute(select(MessageRow)
                                .where(MessageRow.session_id == s1.id))).scalars().all()
    assert [(m.role, m.content) for m in msgs] == [("assistant", "hi human")]


async def test_socket_drop_mid_run_fails_inflight_session():
    from app.config import Settings

    app = create_app(Settings(admin_token="test-token", sqlite_path=":memory:"))
    from app.tokens import TokenService
    sessions = app.state.sessions
    maker = sessions._maker
    async with maker.kw["bind"].begin() as conn:  # schema on the app's own engine
        await conn.run_sync(Base.metadata.create_all)
    await app.state.tokens.ensure_bootstrap("test-token")
    row = await sessions.create("omp-local", "owner1", None)
    sessions.inflight[row.id] = "req_1"
    await sessions._save_status(row.id, "running")

    # register, then the adapter dies: no further frames → disconnect
    frames = [
        {"protocol_version": 1, "type": "agent.register",
         "payload": {"agent_id": "omp-local", "name": "local", "capabilities": []}},
    ]
    await ws_agent(_AgentSocket(app, frames))

    assert not app.state.registry.online("omp-local")
    assert row.id not in sessions.inflight
    assert (await sessions.get(row.id)).status == "failed"


def test_register_sets_projection_flag_and_bad_version_refused():
    client = make_client("secret")
    with client:
        # owns_sessions capability flips the hub into projection mode
        with client.websocket_connect(
            "/ws/agent", headers={"authorization": "Bearer secret"},
        ) as ws:
            ws.send_json({"protocol_version": 1, "type": "agent.register",
                          "payload": {"agent_id": "omp-owner",
                                      "capabilities": ["chat", "owns_sessions"]}})
            deadline = 2.0
            while deadline > 0:
                if client.app.state.registry.is_projection("omp-owner"):
                    break
                time.sleep(0.05); deadline -= 0.05
            assert client.app.state.registry.is_projection("omp-owner")
            ws.close()
            time.sleep(0.2)

        # wrong protocol version must be refused outright
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/ws/agent", headers={"authorization": "Bearer secret"},
            ) as ws:
                ws.send_json({"protocol_version": 99, "type": "agent.register",
                              "payload": {"agent_id": "omp-future",
                                          "capabilities": ["owns_sessions"]}})
                while True:  # drain; receive_text raises on the close frame
                    ws.receive_text()
        assert exc_info.value.code == 1008
        assert not client.app.state.registry.online("omp-future")
        assert not client.app.state.registry.is_projection("omp-future")


def test_sync_push_roundtrip_and_proactive_ack():
    client = make_client("secret")
    with client:
        with client.websocket_connect(
            "/ws/agent", headers={"authorization": "Bearer secret"},
        ) as ws:
            ws.send_json({"protocol_version": 1, "type": "agent.register",
                          "payload": {"agent_id": "omp-local",
                                      "name": "OMP Local",
                                      "capabilities": ["owns_sessions"]}})
            ack = ws.receive_json()  # proactive ack on projection register
            assert ack["type"] == "sync.ack"
            assert ack["payload"]["cursor"] == 0
            ws.send_json({"protocol_version": 1, "type": "sync.push",
                          "payload": {"instance": "j-1", "cursor_start": 0,
                                      "cursor_end": 1, "events": [
                                          {"kind": "session_upserted",
                                           "local_id": "omp-abc",
                                           "omp": "omp-abc", "ws": None,
                                           "status": "idle"}]}})
            ack = ws.receive_json()
            assert ack["payload"]["cursor"] == 1
            assert ack["payload"]["applied"] == 1
            assert any(c["omp_session_id"] == "omp-abc"
                       for c in ack["payload"]["catalog"])


def test_sync_need_returns_pulled_messages_and_done():
    client = make_client("secret")
    with client:
        with client.websocket_connect(
            "/ws/agent", headers={"authorization": "Bearer secret"},
        ) as ws:
            ws.send_json({"protocol_version": 1, "type": "agent.register",
                          "payload": {"agent_id": "omp-local",
                                      "name": "OMP Local",
                                      "capabilities": ["owns_sessions"]}})
            ack = ws.receive_json()  # proactive ack on projection register
            assert ack["type"] == "sync.ack"
            ws.send_json({"protocol_version": 1, "type": "sync.push",
                          "payload": {"instance": "j-1", "cursor_start": 0,
                                      "cursor_end": 2, "events": [
                                          {"kind": "session_upserted",
                                           "local_id": "omp-abc",
                                           "omp": "omp-abc", "ws": None,
                                           "status": "idle"},
                                          {"kind": "message",
                                           "local_id": "omp-abc",
                                           "role": "user",
                                           "content": "hello",
                                           "ts": "2026-09-18T00:00:00+00:00",
                                           "request_id": None}]}})
            ack = ws.receive_json()
            assert ack["payload"]["applied"] == 2
            ws.send_json({"protocol_version": 1, "type": "sync.need",
                          "payload": {"omp_session_id": "omp-abc",
                                      "after_message_id": 0}})
            msgs = ws.receive_json()
            assert msgs["type"] == "sync.messages"
            assert msgs["payload"]["omp_session_id"] == "omp-abc"
            assert msgs["payload"]["done"] is True
            assert [m["content"]
                    for m in msgs["payload"]["messages"]] == ["hello"]


async def test_token_pinned_agent_cannot_register_other_identity():
    from app.config import Settings

    app = create_app(Settings(admin_token="bootstrap", sqlite_path=":memory:"))
    async with app.state.tokens._maker.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await app.state.tokens.ensure_bootstrap("bootstrap")
    _, secret = await app.state.tokens.issue("omp-local")

    frames = [
        {"protocol_version": 1, "type": "agent.register",
         "payload": {"agent_id": "impostor", "name": "impostor",
                     "capabilities": []}},
    ]
    socket = _AgentSocket(app, frames)
    await ws_agent(socket)

    assert not app.state.registry.online("impostor")
    assert not app.state.registry.online("omp-local")


async def test_revoked_token_rejected_on_reconnect():
    from app.config import Settings

    app = create_app(Settings(admin_token="bootstrap", sqlite_path=":memory:"))
    async with app.state.tokens._maker.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await app.state.tokens.ensure_bootstrap("bootstrap")
    _, secret = await app.state.tokens.issue("omp-local")

    reg_frames = [
        {"protocol_version": 1, "type": "agent.register",
         "payload": {"agent_id": "omp-local", "name": "local",
                     "capabilities": []}},
    ]
    first = _AgentSocket(app, reg_frames)
    first.headers = {"authorization": f"Bearer {secret}"}
    await ws_agent(first)
    # accepted: the normal disconnect path closes with 1000, never 1008
    assert 1008 not in first.closed_codes

    await app.state.tokens.revoke(agent_id="omp-local")

    # reconnect with the now-revoked secret: refused
    second = _AgentSocket(app, reg_frames)
    second.headers = {"authorization": f"Bearer {secret}"}
    await ws_agent(second)
    assert second.closed_codes == [1008]


async def test_register_carries_metadata():
    from app.config import Settings

    app = create_app(Settings(admin_token="test-token", sqlite_path=":memory:"))
    registry = app.state.registry
    async with app.state.sessions._maker.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await app.state.tokens.ensure_bootstrap("test-token")

    sock = _AgentSocket(app, [
        {"protocol_version": 1, "type": "agent.register",
         "payload": {"agent_id": "meta-agent", "name": "M",
                     "capabilities": ["chat"],
                     "description": "does X",
                     "tools": ["t1"],
                     "environment": {"ws": "/tmp"}}},
    ])
    await ws_agent(sock)
    row = [r for r in await registry.list() if r.id == "meta-agent"][0]
    assert row.description == "does X"
    assert json.loads(row.tools) == ["t1"]
    assert json.loads(row.environment) == {"ws": "/tmp"}
