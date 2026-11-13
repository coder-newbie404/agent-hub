import asyncio
import json

import pytest

from omp_adapter.client import OMPClient, next_backoff
from omp_adapter.config import Settings
from omp_adapter.protocol import (AGENT_REGISTER, DELEGATE_MESSAGE,
                                  DELEGATION_RESULT, Envelope)


class FakeAgent:
    def __init__(self):
        self.handled: list[Envelope] = []
        self.connects = 0
        self.stopped = False

    async def sync_loop(self) -> None:
        await asyncio.Event().wait()

    async def handle(self, env: Envelope) -> None:
        self.handled.append(env)

    async def on_connect(self) -> None:
        self.connects += 1

    async def stop_work(self) -> None:
        self.stopped = True


class FakeWS:
    def __init__(self, incoming: list[dict] | None = None):
        self.sent: list[dict] = []
        self._incoming = list(incoming or [])

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if not self._incoming:
            raise ConnectionError("closed")
        return json.dumps(self._incoming.pop(0))


def make_client() -> OMPClient:
    return OMPClient(Settings(manager_url="wss://x", agent_token="t",
                              default_workspace="/tmp"), FakeAgent())


def test_backoff_sequence_capped():
    assert [next_backoff(i) for i in range(4)] == [1, 2, 4, 8]
    assert next_backoff(20) == 30


async def test_session_registers_connects_and_survives_disconnect():
    settings = Settings(manager_url="wss://x", agent_token="t",
                        default_workspace="/tmp")
    agent = FakeAgent()
    ws = FakeWS(incoming=[{"protocol_version": 1, "type": "session.create",
                           "request_id": "req_1", "session_id": "sess_1",
                           "payload": {}}])
    client = OMPClient(settings, agent)
    await client.session(ws)  # exits when the fake socket closes
    assert ws.sent[0]["type"] == AGENT_REGISTER
    assert ws.sent[0]["payload"]["agent_id"] == settings.agent_id
    # projection mode: the hub must know this adapter owns its sessions
    assert "owns_sessions" in ws.sent[0]["payload"]["capabilities"]
    assert agent.connects == 1
    assert agent.handled[0].type == "session.create"
    # locally-owned runs survive hub disconnects; the reconnect snapshot
    # reconciles the hub projection
    assert agent.stopped is False


class ExplodingAgent(FakeAgent):
    async def handle(self, env: Envelope) -> None:
        await super().handle(env)
        raise OSError("handler exploded")


async def test_session_propagates_handler_errors():
    settings = Settings(manager_url="wss://x", agent_token="t",
                        default_workspace="/tmp")
    agent = ExplodingAgent()
    ws = FakeWS(incoming=[{"protocol_version": 1, "type": "session.create",
                           "request_id": "req_1", "session_id": "sess_1",
                           "payload": {}}])
    with pytest.raises(OSError):
        await OMPClient(settings, agent).session(ws)
    assert agent.stopped is False


@pytest.mark.asyncio
async def test_call_resolves_on_matching_key():
    client = make_client()
    sent = []

    class FakeWS:
        async def send(self, raw):
            sent.append(json.loads(raw))

    client.ws = FakeWS()
    env = Envelope(type=DELEGATE_MESSAGE, request_id="r1", session_id="s1",
                   payload={"text": "hi"})
    task = asyncio.create_task(client.call(env, timeout=1.0))
    await asyncio.sleep(0)
    # hub's answer: same (session_id, request_id), any type
    assert client.resolve(Envelope(type=DELEGATION_RESULT, request_id="r1",
                                   session_id="s1",
                                   payload={"status": "completed"}))
    result = await task
    assert result.payload["status"] == "completed"
    assert sent[0]["type"] == "delegate.message"


@pytest.mark.asyncio
async def test_call_times_out():
    client = make_client()

    class FakeWS:
        async def send(self, raw):
            pass

    client.ws = FakeWS()
    with pytest.raises(TimeoutError):
        await client.call(Envelope(type=DELEGATE_MESSAGE, request_id="r2",
                                   session_id="s2", payload={}),
                          timeout=0.05)


def test_resolve_ignores_unmatched():
    client = make_client()
    assert not client.resolve(Envelope(type="whatever", request_id="x",
                                       session_id="y", payload={}))


class HangingWS:
    """Accepts sends; recv blocks until closed, then drops the session."""

    def __init__(self):
        self.sent: list[dict] = []
        self._closed = asyncio.Event()

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        await self._closed.wait()
        raise ConnectionError("closed")

    def close(self) -> None:
        self._closed.set()


@pytest.mark.asyncio
async def test_call_fails_promptly_on_disconnect():
    client = make_client()
    ws = HangingWS()
    session_task = asyncio.create_task(client.session(ws))
    while client.ws is None:
        await asyncio.sleep(0)
    call_task = asyncio.create_task(
        client.call(Envelope(type=DELEGATE_MESSAGE, request_id="r9",
                             session_id="s9", payload={}), timeout=30))
    await asyncio.sleep(0)  # call() registers its pending future and sends
    ws.close()
    # the drain fires at disconnect; a 1s ceiling proves it is not the
    # 30s call timeout doing the work
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(call_task, timeout=1)
    await asyncio.wait_for(session_task, timeout=1)
    assert not client._pending  # no stale key survives reconnection
