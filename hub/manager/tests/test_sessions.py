import asyncio
from datetime import timedelta

from sqlalchemy import text, update
import pytest

from app.models import SessionRow, utcnow
from app.protocol import (AGENT_MESSAGE, Envelope, SESSION_CREATE,
                          SESSION_FAILED, SESSION_MESSAGE, SESSION_WAITING)
from app.sessions import (AgentOffline, COMPLETED, FAILED, IDLE, NotFound,
                          RUNNING, RunInProgress, SessionManager,
                          upsert_projected, WAITING)
from tests.fakes import FakeWS


class Recorder:
    def __init__(self):
        self.calls: list[tuple] = []

    async def on_message(self, session_id, text, message_id):
        self.calls.append(("message", session_id, text, message_id))

    async def on_delta(self, session_id, draft):
        self.calls.append(("delta", session_id, draft))

    async def on_activity(self, session_id, label):
        self.calls.append(("activity", session_id, label))

    async def on_completed(self, session_id):
        self.calls.append(("completed", session_id))

    async def on_failed(self, session_id, reason):
        self.calls.append(("failed", session_id, reason))


async def make_mgr(notifier=None):
    import app.db as db
    from app.models import Base
    from app.registry import AgentRegistry
    engine = db.make_engine(":memory:")
    async with engine.begin() as conn:  # schema bootstrap, as in test_db/test_registry
        await conn.run_sync(Base.metadata.create_all)
    maker = db.make_session_maker(engine)
    reg = AgentRegistry(maker)
    mgr = SessionManager(reg, maker, notifier)
    # main.py owns the registry.on_offline wiring (chained with the
    # delegation broker); make_mgr bypasses create_app, so mirror it here.
    reg.on_offline = mgr.on_agent_offline
    return mgr, reg


async def seed_online_agent(reg, ws):
    from app.registry import AgentConnection
    reg._conns["omp-local"] = AgentConnection("omp-local", ws)
    reg._last_seen["omp-local"] = 1e18


async def test_open_session_success_flow():
    ws = FakeWS()
    mgr, reg = await make_mgr()
    await seed_online_agent(reg, ws)

    async def ack():
        await asyncio.sleep(0.01)
        req = ws.sent[0]
        ws_out = Envelope(type=SESSION_WAITING, request_id=req["request_id"],
                          session_id=req["session_id"], payload={"state": "created"})
        reg.get("omp-local").resolve(ws_out)

    task = asyncio.create_task(ack())
    row = await mgr.open_session("omp-local", "owner1", "/tmp/w")
    await task
    assert row.status == "waiting"
    assert ws.sent[0]["type"] == SESSION_CREATE
    assert ws.sent[0]["payload"] == {"workspace": "/tmp/w", "created_by": "owner1"}


async def test_open_session_agent_offline():
    mgr, reg = await make_mgr()
    with pytest.raises(AgentOffline):
        await mgr.open_session("omp-local", "owner1", None)


async def test_user_message_one_in_flight_and_events():
    ws = FakeWS()
    notifier = Recorder()
    mgr, reg = await make_mgr(notifier)
    await seed_online_agent(reg, ws)

    async def ack():
        await asyncio.sleep(0.01)
        req = ws.sent[0]
        reg.get("omp-local").resolve(Envelope(
            type=SESSION_WAITING, request_id=req["request_id"],
            session_id=req["session_id"], payload={"state": "created"}))

    t = asyncio.create_task(ack())
    row = await mgr.open_session("omp-local", "o", "/tmp")
    await t

    req_id = await mgr.user_message(row.id, "hello agent")
    assert ws.sent[-1]["type"] == SESSION_MESSAGE
    assert ws.sent[-1]["payload"] == {"text": "hello agent"}
    with pytest.raises(RunInProgress):
        await mgr.user_message(row.id, "second")

    await mgr.on_agent_message(row.id, "hi human")
    await mgr.on_completed(row.id, "omp-123")
    await asyncio.sleep(0)  # notifier calls are dispatched as tasks
    (msg_call,) = [c for c in notifier.calls if c[0] == "message"]
    assert msg_call[1] == row.id
    assert msg_call[2] == "hi human"  # Discord gets the full reply, not a summary
    # full reply text is still persisted for history
    async with mgr._maker() as s:
        rows = (await s.execute(text("select content from messages"
                                    " where role = 'assistant'"))).scalars().all()
    assert rows == ["hi human"]
    assert ("completed", row.id) in notifier.calls
    got = await mgr.get(row.id)
    assert got.status == "completed" and got.omp_session_id == "omp-123"

    # resume relaxation: completed session accepts a new message
    req2 = await mgr.user_message(row.id, "again")
    assert req2 != req_id
    assert (await mgr.get(row.id)).status == "running"




async def test_session_delta_accumulates_and_finish_clears():
    ws = FakeWS()
    notifier = Recorder()
    mgr, reg = await make_mgr(notifier)
    await seed_online_agent(reg, ws)
    row = await mgr.create("omp-local", "o", "/tmp")

    await mgr.on_session_delta(row.id, None, True)  # reset starts a draft
    await mgr.on_session_delta(row.id, "hello ", False)
    await mgr.on_session_delta(row.id, "world", False)
    await asyncio.sleep(0)
    drafts = [c[2] for c in notifier.calls if c[0] == "delta"]
    assert drafts == ["", "hello ", "hello world"]

    await mgr.on_completed(row.id)
    assert mgr._drafts == {}  # a finished run leaves no draft behind
    assert mgr.activity == {}


async def test_session_activity_labels_and_clear():
    ws = FakeWS()
    notifier = Recorder()
    mgr, reg = await make_mgr(notifier)
    await seed_online_agent(reg, ws)
    row = await mgr.create("omp-local", "o", "/tmp")

    await mgr.on_session_activity(row.id, {"kind": "think"})
    assert mgr.activity[row.id] == "thinking"
    await mgr.on_session_activity(row.id, {"kind": "tool", "state": "start",
                                           "tool": "bash", "detail": "echo hi"})
    assert mgr.activity[row.id] == "bash: echo hi"
    await mgr.on_session_activity(row.id, {"kind": "tool", "state": "end",
                                           "tool": "bash"})
    assert row.id not in mgr.activity  # tool finished → generic working state
    await asyncio.sleep(0)
    labels = [c[2] for c in notifier.calls if c[0] == "activity"]
    assert labels == ["thinking", "bash: echo hi", None]


async def test_rename_and_archive():
    ws = FakeWS()
    mgr, reg = await make_mgr()
    await seed_online_agent(reg, ws)
    row = await mgr.create("omp-local", "o", "/tmp", title="OCR Project")
    assert row.title == "OCR Project"

    row = await mgr.rename(row.id, "Backend Debug")
    assert (await mgr.get(row.id)).title == "Backend Debug"

    await mgr.set_archived(row.id, True)
    assert (await mgr.get(row.id)).archived is True
    await mgr.set_archived(row.id, False)
    assert (await mgr.get(row.id)).archived is False


async def test_on_failed_cancelled_sets_cancelled():
    ws = FakeWS()
    notifier = Recorder()
    mgr, reg = await make_mgr(notifier)
    await seed_online_agent(reg, ws)

    async def ack():
        await asyncio.sleep(0.01)
        req = ws.sent[0]
        reg.get("omp-local").resolve(Envelope(
            type=SESSION_WAITING, request_id=req["request_id"],
            session_id=req["session_id"], payload={"state": "created"}))

    t = asyncio.create_task(ack())
    row = await mgr.open_session("omp-local", "o", "/tmp")
    await t
    await mgr.user_message(row.id, "go")
    await mgr.on_failed(row.id, "cancelled")
    assert (await mgr.get(row.id)).status == "cancelled"
    assert ("failed", row.id, "cancelled") in notifier.calls


async def test_user_message_offline_rejected():
    ws = FakeWS()
    mgr, reg = await make_mgr()
    await seed_online_agent(reg, ws)
    row = await mgr.create("omp-local", "o", "/tmp")
    # no live connection now — clear it
    await reg.mark_offline("omp-local")
    with pytest.raises(AgentOffline):
        await mgr.user_message(row.id, "hi")


async def test_agent_offline_fails_inflight_sessions():
    ws = FakeWS()
    notifier = Recorder()
    mgr, reg = await make_mgr(notifier)
    await seed_online_agent(reg, ws)

    async def ack():
        await asyncio.sleep(0.01)
        req = ws.sent[0]
        reg.get("omp-local").resolve(Envelope(
            type=SESSION_WAITING, request_id=req["request_id"],
            session_id=req["session_id"], payload={"state": "created"}))

    t = asyncio.create_task(ack())
    row = await mgr.open_session("omp-local", "o", "/tmp")
    await t
    await mgr.user_message(row.id, "long run")
    assert row.id in mgr.inflight
    assert (await mgr.get(row.id)).status == "running"

    # adapter dies mid-run: the socket drop takes the agent offline and the
    # offline hook must fail the in-flight session instead of wedging it
    await reg.mark_offline("omp-local")

    assert row.id not in mgr.inflight
    assert (await mgr.get(row.id)).status == "failed"
    await asyncio.sleep(0)  # on_failed is dispatched as a task
    assert ("failed", row.id, "agent offline") in notifier.calls


async def test_delete_removes_session_and_transcript():
    from app.models import MessageRow
    mgr, _ = await make_mgr()
    row = await mgr.create("omp-local", "owner1", "/tmp/w", "t")
    async with mgr._maker() as s:
        s.add(MessageRow(session_id=row.id, role="user", content="hi"))
        await s.commit()

    await mgr.delete(row.id)

    with pytest.raises(NotFound):
        await mgr.get(row.id)
    assert await mgr.message_count(row.id) == 0


async def test_delete_unknown_session_raises():
    mgr, _ = await make_mgr()
    with pytest.raises(NotFound):
        await mgr.delete("nope")


async def test_delete_refuses_inflight_session():
    mgr, _ = await make_mgr()
    row = await mgr.create("omp-local", "owner1", None, None)
    mgr.inflight[row.id] = "req_1"

    with pytest.raises(RunInProgress):
        await mgr.delete(row.id)
    await mgr.get(row.id)  # untouched


async def test_reconcile_imports_upserts_and_resolves_missing():
    from app.models import MessageRow  # noqa: F401  (import guard only)
    mgr, reg = await make_mgr()
    reg.set_projection("omp-local", True)
    row = await mgr.create("omp-local", "owner1", "/old", "t")
    await mgr._save_status(row.id, WAITING, "omp_known")

    await mgr.reconcile_snapshot("omp-local", [
        {"local_id": "omp_known", "status": "running", "workspace": "/new"},
        {"local_id": "omp_foreign", "status": "idle", "title": "Imported"},
    ], done=False)

    updated = await mgr.get(row.id)
    assert updated.status == "running"
    assert updated.workspace == "/new"

    imported = [r for r in await mgr.list() if r.omp_session_id == "omp_foreign"]
    assert len(imported) == 1 and imported[0].status == "waiting"
    assert imported[0].title == "Imported"

    # done: a hub row still 'running' but absent from the snapshot is lost
    lost = await mgr.create("omp-local", "owner1", None, None)
    await mgr._save_status(lost.id, RUNNING, "omp_gone")
    mgr.inflight[lost.id] = "req_1"

    notifier = mgr.notifier
    mgr.notifier = None
    await mgr.reconcile_snapshot("omp-local", [
        {"local_id": "omp_known", "status": "idle"}], done=True)
    mgr.notifier = notifier

    assert (await mgr.get(lost.id)).status == "failed"
    assert lost.id not in mgr.inflight
    # sessions still reported locally survive the done pass
    assert (await mgr.get(row.id)).status == "waiting"


async def test_reconcile_done_spares_active_hub_created_row():
    mgr, reg = await make_mgr()
    reg.set_projection("omp-local", True)
    row = await mgr.create("omp-local", "owner1", None, None)
    await mgr._save_status(row.id, RUNNING, None)  # hub-created: no omp id yet

    await mgr.reconcile_snapshot("omp-local", [
        {"local_id": row.id, "status": "running", "workspace": "/w"},
    ], done=True)

    # reported by hub id in the same batch: not lost
    assert (await mgr.get(row.id)).status == "running"


async def test_reconcile_done_spares_imported_row_seen_by_hub_id():
    mgr, reg = await make_mgr()
    reg.set_projection("omp-local", True)
    row = await mgr.create("omp-local", "owner1", None, None)
    await mgr._save_status(row.id, RUNNING, "omp-x")  # imported: omp id set

    await mgr.reconcile_snapshot("omp-local", [
        {"local_id": row.id, "status": "running"},
    ], done=True)

    # seen only by hub id; the omp id alone would not match
    assert (await mgr.get(row.id)).status == "running"


async def test_offline_projection_agent_keeps_reported_state():
    mgr, reg = await make_mgr()
    reg.set_projection("omp-local", True)
    calls = []

    class PartialNotifier:
        async def on_agent_disconnected(self, agent_id):
            calls.append(("disconnected", agent_id))

    mgr.notifier = PartialNotifier()
    row = await mgr.create("omp-local", "owner1", None, None)
    await mgr._save_status(row.id, RUNNING, "omp_1")
    mgr.inflight[row.id] = "req_1"

    await mgr.on_agent_offline("omp-local")

    # truth unknown: status untouched, inflight cleared, notice dispatched
    assert (await mgr.get(row.id)).status == "running"
    assert row.id not in mgr.inflight
    await asyncio.sleep(0)
    assert calls == [("disconnected", "omp-local")]


async def test_offline_legacy_agent_still_fails_inflight():
    mgr, reg = await make_mgr()  # no projection flag → legacy semantics
    row = await mgr.create("omp-local", "owner1", None, None)
    await mgr._save_status(row.id, RUNNING, "omp_1")
    mgr.inflight[row.id] = "req_1"

    await mgr.on_agent_offline("omp-local")

    assert (await mgr.get(row.id)).status == "failed"


async def test_cancel_acked_for_projection_agent():
    from app.protocol import Envelope, SESSION_CANCEL_ACK
    mgr, reg = await make_mgr()
    reg.set_projection("omp-local", True)
    from app.registry import AgentConnection
    ws = FakeWS()
    reg._conns["omp-local"] = AgentConnection("omp-local", ws)
    row = await mgr.create("omp-local", "owner1", None, None)
    mgr.inflight[row.id] = "req_1"

    async def acker():
        await asyncio.sleep(0.01)
        (session_id, request_id), fut = next(iter(
            reg._conns["omp-local"]._pending.items()))
        reg._conns["omp-local"].resolve(Envelope(
            type=SESSION_CANCEL_ACK, session_id=session_id,
            request_id=request_id, payload={"state": "accepted"}))

    task = asyncio.create_task(acker())
    sent_before = len(ws.sent)
    assert await mgr._cancel_session(row.id, ack_timeout=1.0) is True
    await task
    cancel_frames = [f for f in ws.sent if f["type"] == "session.cancel"]
    assert len(cancel_frames) == 1
    assert cancel_frames[0]["request_id"]


async def test_cancel_unacknowledged_never_fabricates(monkeypatch):
    mgr, reg = await make_mgr()
    reg.set_projection("omp-local", True)
    from app.registry import AgentConnection
    reg._conns["omp-local"] = AgentConnection("omp-local", FakeWS())
    row = await mgr.create("omp-local", "owner1", None, None)
    mgr.inflight[row.id] = "req_1"
    await mgr._save_status(row.id, RUNNING, "omp_1")

    assert await mgr._cancel_session(row.id, ack_timeout=0.01) is True
    # two attempts were made, status untouched — the outcome event decides
    assert (await mgr.get(row.id)).status == "running"
    assert mgr.inflight.get(row.id) == "req_1"


async def test_agent_message_returns_id_and_stores_request_id():
    mgr, _ = await make_mgr()
    row = await mgr.create("omp-local", "o", None)
    mid = await mgr.on_agent_message(row.id, "reply", request_id="req_9")
    msgs = await mgr.messages(row.id)
    assert [m.id for m in msgs] == [mid]
    assert msgs[0].request_id == "req_9"
    assert msgs[0].posted_at is None


async def test_user_message_is_marked_posted_at_insert():
    ws = FakeWS()
    mgr, reg = await make_mgr()
    await seed_online_agent(reg, ws)
    row = await mgr.create("omp-local", "o", None)

    async def ack():
        await asyncio.sleep(0.01)
        req = ws.sent[0]
        reg.get("omp-local").resolve(Envelope(
            type=SESSION_WAITING, request_id=req["request_id"],
            session_id=req["session_id"], payload={"state": "created"}))

    task = asyncio.create_task(ack())
    await mgr.open_session("omp-local", "o", None)
    await task
    await mgr.user_message(row.id, "hello")
    msgs = await mgr.messages(row.id)
    assert msgs[0].role == "user" and msgs[0].posted_at is not None


async def test_missed_queries_and_mark_posted():
    mgr, _ = await make_mgr()
    row = await mgr.create("omp-local", "o", None)
    a = await mgr.on_agent_message(row.id, "one")
    b = await mgr.on_agent_message(row.id, "two")
    assert await mgr.missed_session_ids() == [row.id]
    assert [m.id for m in await mgr.missed_messages(row.id)] == [a, b]
    await mgr.mark_posted([a])
    assert [m.id for m in await mgr.missed_messages(row.id)] == [b]
    assert await mgr.missed_session_ids() == [row.id]
    await mgr.mark_posted([b])
    assert await mgr.missed_session_ids() == []


async def test_resolve_local_matches_id_or_omp_id():
    mgr, _ = await make_mgr()
    await mgr.create("omp-local", "o", None)  # id like sess_xxx
    rows = await mgr.list()
    hub_row = rows[0]
    hub_row.omp_session_id = None
    assert (await mgr.resolve_local("omp-local", hub_row.id)) is not None
    assert (await mgr.resolve_local("omp-local", "omp-xyz")) is None
    imported = await upsert_projected(mgr._maker, "omp-local", "omp-xyz",
                                      status="idle")
    assert (await mgr.resolve_local("omp-local", "omp-xyz")).id == imported.id
    assert imported.omp_session_id == "omp-xyz"


async def test_upsert_projected_is_idempotent():
    mgr, _ = await make_mgr()
    first = await upsert_projected(mgr._maker, "omp-local", "omp-abc",
                                   status="idle", workspace="/w")
    again = await upsert_projected(mgr._maker, "omp-local", "omp-abc",
                                   status="idle", workspace="/w2")
    assert first.id == again.id and again.workspace == "/w2"
    assert len(await mgr.list()) == 1


async def test_upsert_projected_maps_adapter_vocabulary():
    """Snapshot/sync events speak adapter ("idle"/"running"/"failed"); the
    projection stores hub vocabulary. `idle`+omp = completed (the session
    ran to completion), `idle` without omp = waiting (never ran)."""
    mgr, _ = await make_mgr()
    ran = await upsert_projected(mgr._maker, "omp-local", "omp-ran",
                                 status="idle", omp="omp-ran-1")
    assert ran.status == "completed"
    fresh = await upsert_projected(mgr._maker, "omp-local", "omp-fresh",
                                   status="idle")
    assert fresh.status == "waiting"
    assert (await upsert_projected(mgr._maker, "omp-local", "omp-ran",
                                   status="running")).status == "running"
    assert (await upsert_projected(mgr._maker, "omp-local", "omp-bad",
                                   status="failed")).status == "failed"


async def test_upsert_projected_repairs_raw_idle_rows():
    """IDLE is a legal hub state (the lifecycle sweeper writes it), so a
    projection touch must no longer overwrite a row carrying it; a raw
    "idle" artifact from a pre-sweeper restore self-heals only on its
    next running touch."""
    mgr, _ = await make_mgr()
    row = await upsert_projected(mgr._maker, "omp-local", "omp-x",
                                 status="idle")
    import app.db as db
    async with mgr._maker() as s:
        await s.execute(text("UPDATE sessions SET status='idle'"
                             " WHERE id=:i"), {"i": row.id})
        await s.commit()
    touched = await upsert_projected(mgr._maker, "omp-local", "omp-x",
                                     status="idle", omp="omp-x-1")
    assert touched.status == IDLE
    healed = await upsert_projected(mgr._maker, "omp-local", "omp-x",
                                    status="running")
    assert healed.status == RUNNING


async def test_upsert_projected_failed_applies_to_idled_row():
    """A failed report must land even on a hub-idled row: the hub can miss
    the whole RUNNING window (restart spanning an adapter-local run), and
    the row would otherwise display idle forever."""
    mgr, _ = await make_mgr()
    row = await upsert_projected(mgr._maker, "omp-local", "omp-f",
                                 status="idle", omp="omp-f-1")
    import app.db as db
    async with mgr._maker() as s:
        await s.execute(text("UPDATE sessions SET status='idle'"
                             " WHERE id=:i"), {"i": row.id})
        await s.commit()
    failed = await upsert_projected(mgr._maker, "omp-local", "omp-f",
                                    status="failed", omp="omp-f-1")
    assert failed.status == FAILED


async def _backdate(mgr, session_id, hours=0, days=0):
    """Force last_used_at into the past so time-based rules can fire."""
    async with mgr._maker() as s:
        await s.execute(update(SessionRow).where(SessionRow.id == session_id)
                        .values(last_used_at=utcnow()
                                - timedelta(hours=hours, days=days)))
        await s.commit()


@pytest.mark.asyncio
async def test_sweep_lifecycle_transitions():
    mgr, _ = await make_mgr()
    # old completed session → idle
    r1 = await mgr.create("agent-b", None, None)
    await mgr._save_status(r1.id, COMPLETED)
    await _backdate(mgr, r1.id, hours=12)
    # fresh completed session (1 h old, inside the 6 h cut) → untouched
    r2 = await mgr.create("agent-b", None, None)
    await mgr._save_status(r2.id, COMPLETED)
    await _backdate(mgr, r2.id, hours=1)
    # ancient completed session → idled, then archived by the same sweep
    r3 = await mgr.create("agent-b", None, None)
    await mgr._save_status(r3.id, COMPLETED)
    await _backdate(mgr, r3.id, days=20)
    # failed session → never touched
    r4 = await mgr.create("agent-b", None, None)
    await mgr._save_status(r4.id, FAILED)
    await _backdate(mgr, r4.id, days=20)
    # never used (last_used_at NULL) → untouched by both rules
    r5 = await mgr.create("agent-b", None, None)

    counts = await mgr.sweep_lifecycle(idle_after_s=6 * 3600,
                                       archive_after_s=14 * 86400)
    assert (await mgr.get(r1.id)).status == IDLE
    assert (await mgr.get(r2.id)).status == COMPLETED
    got3 = await mgr.get(r3.id)
    assert got3.status == IDLE and got3.archived is True
    assert (await mgr.get(r4.id)).status == FAILED
    got5 = await mgr.get(r5.id)
    assert got5.last_used_at is None and got5.archived is False
    assert counts == {"idled": 2, "archived": 1}


@pytest.mark.asyncio
async def test_sweep_never_idles_inflight():
    ws = FakeWS()
    mgr, reg = await make_mgr()
    await seed_online_agent(reg, ws)
    r = await mgr.create("omp-local", None, None)
    await mgr.user_message(r.id, "x")   # sets inflight + RUNNING
    await _backdate(mgr, r.id, hours=12)
    await mgr.sweep_lifecycle(idle_after_s=6 * 3600,
                              archive_after_s=14 * 86400)
    assert (await mgr.get(r.id)).status == RUNNING


async def test_create_with_creator_and_description():
    mgr, _ = await make_mgr()
    row = await mgr.create("agent-b", None, None, title=None,
                           creator_agent_id="agent-a",
                           description="fix the flaky test")
    assert row.owner_id is None
    assert row.creator_agent_id == "agent-a"
    assert row.description == "fix the flaky test"
    got = await mgr.get(row.id)
    assert got.last_used_at is None


async def test_user_message_bumps_last_used_at():
    ws = FakeWS()
    mgr, reg = await make_mgr()
    from app.registry import AgentConnection
    reg._conns["agent-b"] = AgentConnection("agent-b", ws)
    reg._last_seen["agent-b"] = 1e18
    row = await mgr.create("agent-b", None, None)
    await mgr.user_message(row.id, "hello", request_id="req_custom")
    got = await mgr.get(row.id)
    assert got.last_used_at is not None


async def test_touch_sets_last_used_at():
    mgr, _ = await make_mgr()
    row = await mgr.create("agent-b", None, None)
    await mgr.touch(row.id)
    assert (await mgr.get(row.id)).last_used_at is not None
