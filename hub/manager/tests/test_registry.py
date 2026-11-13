import asyncio
import json
import pytest

from app.protocol import AGENT_REGISTER, Envelope, SESSION_CREATE
from app.models import Base
from app.registry import AgentConnection, AgentRegistry
from tests.fakes import FakeWS


async def make_registry() -> AgentRegistry:
    import app.db as db
    engine = db.make_engine(":memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return AgentRegistry(db.make_session_maker(engine))


async def test_register_upserts_and_is_online():
    reg = await make_registry()
    conn = await reg.register("omp-local", "OMP Local", ["chat"], FakeWS())
    assert reg.online("omp-local") and reg.get("omp-local") is conn
    rows = await reg.list()
    assert rows[0].status == "online"
    assert json.loads(rows[0].capabilities) == ["chat"]

async def test_call_resolves_matching_response():
    reg = await make_registry()
    ws = FakeWS()
    conn = await reg.register("omp-local", "OMP Local", [], ws)

    async def replier():
        await asyncio.sleep(0.01)
        req = ws.sent[0]
        conn.resolve(Envelope(type="session.waiting", request_id=req["request_id"],
                              session_id=req["session_id"], payload={"state": "created"}))

    task = asyncio.create_task(replier())
    resp = await reg.get("omp-local").call(
        Envelope(type=SESSION_CREATE, request_id="req_1", session_id="sess_1"))
    await task
    assert resp.payload == {"state": "created"}


async def test_mark_offline_and_sweep():
    reg = await make_registry()
    await reg.register("omp-local", "OMP Local", [], FakeWS())
    await reg.mark_offline("omp-local")
    assert not reg.online("omp-local")
    assert (await reg.list())[0].status == "offline"
    # sweep: fresh registration survives, stale agent goes offline
    await reg.register("omp-local", "OMP Local", [], FakeWS())
    await reg.register("old", "Old", [], FakeWS())
    import time as _t
    reg._last_seen["old"] = _t.monotonic() - 120
    gone = await reg.sweep(offline_after=90)
    assert gone == ["old"]



async def test_closed_connection_fails_fast_on_send():
    conn = AgentConnection("omp-local", FakeWS())
    await conn.close()
    assert conn.closed
    with pytest.raises(ConnectionError):
        await conn.send(Envelope(type=SESSION_CREATE, request_id="req_1",
                                 session_id="sess_1"))


async def test_register_replaces_connection_once():
    reg = await make_registry()
    old_ws = FakeWS()
    old = await reg.register("omp-local", "OMP Local", [], old_ws)
    new_ws = FakeWS()
    new = await reg.register("omp-local", "OMP Local", [], new_ws)
    assert old.closed and old_ws.closed
    assert not new.closed and not new_ws.closed
    assert reg.get("omp-local") is new
    assert reg.online("omp-local")
    assert len(await reg.list()) == 1


async def test_mark_offline_if_current_ignores_stale_connection():
    reg = await make_registry()
    stale_ws = FakeWS()
    stale = await reg.register("omp-local", "OMP Local", [], stale_ws)
    new_ws = FakeWS()
    current = await reg.register("omp-local", "OMP Local", [], new_ws)

    # a slow old receive loop unwinds after the adapter re-registered:
    # its finally must not close the new connection
    await reg.mark_offline_if_current(stale)
    assert reg.get("omp-local") is current
    assert reg.online("omp-local")
    assert not new_ws.closed

    # the genuine owner still takes the agent offline
    await reg.mark_offline_if_current(current)
    assert not reg.online("omp-local")
    assert new_ws.closed


async def test_capabilities_returns_declared_set():
    reg = await make_registry()
    await reg.register("omp", "OMP", ["chat", "cancel"], FakeWS())
    assert await reg.capabilities("omp") == {"chat", "cancel"}
    assert await reg.capabilities("unknown") == set()


async def test_register_stores_metadata():
    reg = await make_registry()
    await reg.register("a1", "Agent One", ["chat"], FakeWS(),
                       description="good at reports",
                       tools=[{"name": "sql"}], environment={"os": "linux"})
    rows = await reg.list()
    assert rows[0].description == "good at reports"
    assert json.loads(rows[0].tools) == [{"name": "sql"}]
    assert json.loads(rows[0].environment) == {"os": "linux"}


async def test_register_defaults_empty_metadata():
    reg = await make_registry()
    await reg.register("a2", "A2", [], FakeWS())
    rows = await reg.list()
    assert rows[0].description == ""
    assert json.loads(rows[0].tools) == []
    assert json.loads(rows[0].environment) == {}
