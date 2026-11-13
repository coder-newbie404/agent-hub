import json
from datetime import datetime, timedelta

from sqlalchemy import select, update

from app.models import MessageRow, SessionRow
from app.sessions import SessionManager, upsert_projected
from app.sync import SyncManager


async def make(maker_pair=None):
    import app.db as db
    from app.models import Base
    engine = db.make_engine(":memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = db.make_session_maker(engine)
    sessions = SessionManager(maker, maker)
    return SyncManager(maker), sessions, maker


async def test_apply_imports_session_and_inserts_missed_message():
    sync, sessions, maker = await make()
    res = await sync.apply("omp-local", "j-1", 0, [
        {"kind": "session_upserted", "local_id": "omp-abc", "omp": "omp-abc",
         "ws": "/w", "status": "idle"},
        {"kind": "message", "local_id": "omp-abc", "role": "user",
         "content": "hello", "ts": "2026-09-18T00:00:00+00:00",
         "request_id": None},
    ])
    assert res["instance"] == "j-1" and res["cursor"] == 2
    assert res["applied"] == 2
    row = await sessions.resolve_local("omp-local", "omp-abc")
    assert row is not None and row.workspace == "/w"
    missed = await sessions.missed_messages(row.id)
    assert len(missed) == 1 and missed[0].role == "user"
    assert missed[0].posted_at is None


async def test_apply_skips_request_id_duplicates_but_advances_cursor():
    sync, sessions, maker = await make()
    row = await sessions.create("omp-local", "o", None)
    await sessions.on_agent_message(row.id, "live reply", request_id="req_1")
    res = await sync.apply("omp-local", "j-1", 0, [
        {"kind": "message", "local_id": row.id, "role": "assistant",
         "content": "live reply", "ts": "2026-09-18T00:00:00+00:00",
         "request_id": "req_1"},
        {"kind": "run_started", "local_id": row.id, "pid": 1},  # ignored kind
    ])
    assert res["applied"] == 0 and res["cursor"] == 2
    assert len(await sessions.messages(row.id)) == 1


async def test_apply_rejects_cursor_gap_and_reports_stored():
    sync, sessions, maker = await make()
    res = await sync.apply("omp-local", "j-1", 5, [])
    assert res["cursor"] == 0 and res["applied"] == 0


async def test_instance_mismatch_resets_cursor():
    sync, sessions, maker = await make()
    await sync.apply("omp-local", "j-1", 0, [
        {"kind": "run_started", "local_id": "x", "pid": 1}])
    assert (await sync.stored("omp-local")) == ("j-1", 1)
    # journal wiped: new instance, adapter restarts at 0 — hub follows
    res = await sync.apply("omp-local", "j-2", 0, [
        {"kind": "session_upserted", "local_id": "omp-new", "omp": "omp-new",
         "ws": None, "status": "idle"}])
    assert (await sync.stored("omp-local")) == ("j-2", 1)
    assert await sessions.resolve_local("omp-local", "omp-new") is not None


async def test_catalog_reports_counts_and_last_id():
    sync, sessions, maker = await make()
    await upsert_projected(maker, "omp-local", "omp-abc", status="idle")
    row = await sessions.resolve_local("omp-local", "omp-abc")
    await sessions.on_agent_message(row.id, "a", request_id="req_1")
    await sessions.on_agent_message(row.id, "b", request_id="req_2")
    cat = (await sync.ack_payload("omp-local"))["catalog"]
    assert cat == [{"omp_session_id": "omp-abc", "hub_session_id": row.id,
                    "message_count": 2, "last_message_id": cat[0]["last_message_id"]}]
    assert cat[0]["last_message_id"] > 0


async def test_catalog_skips_unbound_hub_created_rows():
    """Hub-created rows the adapter has not confirmed (NULL omp id) are
    hub-internal: advertising them made the adapter adopt ghost local
    sessions keyed by hub ids."""
    sync, sessions, maker = await make()
    unbound = await sessions.create("omp-local", "o", "/w")
    await upsert_projected(maker, "omp-local", "omp-abc", status="idle",
                           workspace="/w", omp="omp-abc")
    cat = (await sync.ack_payload("omp-local"))["catalog"]
    assert [e["omp_session_id"] for e in cat] == ["omp-abc"]
    assert unbound.id not in {e["omp_session_id"] for e in cat}


async def test_apply_session_titled_sets_title():
    sync, sessions, maker = await make()
    res = await sync.apply("omp-local", "j-1", 0, [
        {"kind": "session_upserted", "local_id": "omp-abc", "omp": "omp-abc",
         "ws": "/w", "status": "idle"},
        {"kind": "session_titled", "local_id": "omp-abc",
         "title": "Fix login flow"},
    ])
    row = await sessions.resolve_local("omp-local", "omp-abc")
    assert row.title == "Fix login flow"
    assert res["applied"] == 2
    # blank title is a no-op that still advances the cursor
    res = await sync.apply("omp-local", "j-1", 2, [
        {"kind": "session_titled", "local_id": "omp-abc", "title": "   "},
    ])
    assert res["applied"] == 0 and res["cursor"] == 3
    row = await sessions.resolve_local("omp-local", "omp-abc")
    assert row.title == "Fix login flow"


async def test_upsert_projected_touch_bumps_updated_at():
    sync, sessions, maker = await make()
    row = await upsert_projected(maker, "omp-local", "omp-abc",
                                 status="idle", workspace="/w")
    old = row.updated_at - timedelta(days=1)
    async with maker() as s:
        await s.execute(update(SessionRow).where(SessionRow.id == row.id)
                        .values(updated_at=old))
        await s.commit()
    again = await upsert_projected(maker, "omp-local", "omp-abc",
                                   status="idle", workspace="/w")
    assert again.updated_at > old


async def test_apply_message_bumps_session_updated_at():
    sync, sessions, maker = await make()
    await sync.apply("omp-local", "j-1", 0, [
        {"kind": "session_upserted", "local_id": "omp-abc", "omp": "omp-abc",
         "ws": "/w", "status": "idle"},
    ])
    row = await sessions.resolve_local("omp-local", "omp-abc")
    async with maker() as s:
        await s.execute(update(SessionRow).where(SessionRow.id == row.id)
                        .values(updated_at=datetime(2026, 9, 18)))
        await s.commit()
    await sync.apply("omp-local", "j-1", 1, [
        {"kind": "message", "local_id": "omp-abc", "role": "user",
         "content": "hello", "ts": "2026-09-19T00:00:00+00:00",
         "request_id": None},
    ])
    refreshed = await sessions.resolve_local("omp-local", "omp-abc")
    assert refreshed.updated_at > datetime(2026, 9, 18)


async def test_on_applied_called_with_touched_sessions():
    seen = []

    async def on_applied(agent_id, session_ids):
        seen.append((agent_id, session_ids))

    import app.db as db
    from app.models import Base
    engine = db.make_engine(":memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = db.make_session_maker(engine)
    sessions = SessionManager(maker, maker)
    sync = SyncManager(maker, on_applied)
    await sync.apply("omp-local", "j-1", 0, [
        {"kind": "message", "local_id": "omp-abc", "role": "user",
         "content": "x", "ts": "2026-09-18T00:00:00+00:00",
         "request_id": None}])
    assert seen and seen[0][0] == "omp-local" and len(seen[0][1]) == 1


async def test_apply_skips_mirrored_events_but_advances_cursor():
    sync, sessions, maker = await make()
    row = await sessions.create("omp-local", "o", None)
    await sessions.on_agent_message(row.id, "local", request_id="req_1")
    # era 1 establishes stored state; the mirrored echo arrives in era 2
    # (matched cursor) — exactly how a post-pull re-push looks in prod
    await sync.apply("omp-local", "j-1", 0, [
        {"kind": "run_started", "local_id": row.id, "pid": 1}])
    res = await sync.apply("omp-local", "j-1", 1, [
        {"kind": "message", "local_id": row.id, "role": "assistant",
         "content": "echo of a hub row", "ts": "2026-09-18T00:00:00+00:00",
         "request_id": None, "mirrored": True}])
    assert res["applied"] == 0 and res["cursor"] == 2
    assert [(m.role, m.content) for m in
            await sessions.messages(row.id)] == [("assistant", "local")]


async def test_respond_need_serves_messages_after_cursor():
    sync, sessions, maker = await make()
    await upsert_projected(maker, "omp-local", "omp-abc", status="idle")
    row = await sessions.resolve_local("omp-local", "omp-abc")
    await sessions.on_agent_message(row.id, "a", request_id="r1")
    await sessions.on_agent_message(row.id, "b", request_id="r2")
    batch = await sync.respond_need("omp-local", "omp-abc", 0)
    assert batch["hub_session_id"] == row.id
    assert batch["done"] is True  # single batch under the 200 cap
    assert [m["content"] for m in batch["messages"]] == ["a", "b"]
    assert batch["messages"][0]["id"] > 0
    tail = await sync.respond_need("omp-local", "omp-abc",
                                   batch["messages"][0]["id"])
    assert [m["content"] for m in tail["messages"]] == ["b"]


async def test_respond_need_unknown_session_is_empty_done():
    sync, sessions, maker = await make()
    batch = await sync.respond_need("omp-local", "omp-ghost", 0)
    assert batch == {"omp_session_id": "omp-ghost", "hub_session_id": None,
                     "messages": [], "done": True}


async def test_adoption_restores_mirrored_rows_as_rendered():
    """Wiped-hub restore: an instance-mismatch push must bring mirrored
    rows back (they are this DB's only copy), marked already-rendered —
    while normal matched-cursor operation still skips them."""
    sync, sessions, maker = await make()
    # era 1: the hub holds the row and mirrors it out to the journal
    await sync.apply("omp-local", "j-1", 0, [
        {"kind": "session_upserted", "local_id": "omp-abc", "omp": "omp-abc",
         "ws": None, "status": "idle"}])
    row = await sessions.resolve_local("omp-local", "omp-abc")
    await sync.apply("omp-local", "j-1", 1, [
        {"kind": "message", "local_id": "omp-abc", "role": "user",
         "content": "hello", "ts": "2026-09-18T00:00:00+00:00",
         "request_id": None, "mirrored": True}])
    assert await sessions.messages(row.id) == []  # skipped: hub's own row
    # hub DB wiped: new fresh hub ("", 0) sees instance j-1 → adoption
    import app.db as db
    from app.models import Base
    engine = db.make_engine(":memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    fresh = db.make_session_maker(engine)
    sync2 = SyncManager(fresh)
    sessions2 = SessionManager(fresh, fresh)
    res = await sync2.apply("omp-local", "j-1", 0, [
        {"kind": "session_upserted", "local_id": "omp-abc", "omp": "omp-abc",
         "ws": None, "status": "idle"},
        {"kind": "message", "local_id": "omp-abc", "role": "user",
         "content": "hello", "ts": "2026-09-18T00:00:00+00:00",
         "request_id": None, "mirrored": True}])
    assert res["applied"] == 2
    row2 = await sessions2.resolve_local("omp-local", "omp-abc")
    restored = await sessions2.missed_messages(row2.id)
    assert restored == []  # restored as already-rendered
    msgs = await sessions2.messages(row2.id)
    assert len(msgs) == 1 and msgs[0].posted_at == msgs[0].created_at
