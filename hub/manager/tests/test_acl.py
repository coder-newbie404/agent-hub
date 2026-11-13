import pytest

from app.acl import can_read, can_write, visible_sessions
from app.models import SessionGrantRow, SessionRow


@pytest.fixture
async def make_maker():
    # same in-memory DB bootstrap as make_mgr in test_sessions.py
    import app.db as db
    from app.models import Base
    engine = db.make_engine(":memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return db.make_session_maker(engine)


async def _seed(maker, **kw):
    async with maker() as s:
        row = SessionRow(id=kw.get("id", "sess_1"),
                         agent_id=kw.get("agent_id", "worker"),
                         owner_id=kw.get("owner_id"),
                         creator_agent_id=kw.get("creator"),
                         status="waiting")
        s.add(row)
        if kw.get("grantee"):
            s.add(SessionGrantRow(session_id=row.id, agent_id=kw["grantee"],
                                  level=kw.get("level", "read"),
                                  granted_by=kw.get("creator", "creator-a")))
        await s.commit()
        return row


@pytest.mark.asyncio
async def test_creator_has_implicit_write(make_maker):
    maker = make_maker
    row = await _seed(maker, creator="creator-a")
    assert await can_write(maker, "creator-a", row)
    assert await can_read(maker, "creator-a", row)


@pytest.mark.asyncio
async def test_grant_levels(make_maker):
    maker = make_maker
    row = await _seed(maker, creator="creator-a", grantee="reader",
                      level="read")
    assert await can_read(maker, "reader", row)
    assert not await can_write(maker, "reader", row)
    row2 = await _seed(maker, id="sess_2", creator="creator-a",
                       grantee="writer", level="write")
    assert await can_write(maker, "writer", row2)
    assert await can_read(maker, "writer", row2)  # read ⊂ write


@pytest.mark.asyncio
async def test_stranger_sees_nothing(make_maker):
    maker = make_maker
    row = await _seed(maker, creator="creator-a")
    assert not await can_read(maker, "stranger", row)
    assert await visible_sessions(maker, "stranger") == []


@pytest.mark.asyncio
async def test_hosted_created_granted_union(make_maker):
    maker = make_maker
    await _seed(maker, id="s_host", agent_id="me", owner_id="discord-1")
    await _seed(maker, id="s_created", agent_id="other", creator="me")
    await _seed(maker, id="s_granted", agent_id="other", creator="someone",
                grantee="me", level="read")
    await _seed(maker, id="s_other", agent_id="other", creator="someone")
    rows = {r.id for r in await visible_sessions(maker, "me")}
    assert rows == {"s_host", "s_created", "s_granted"}


@pytest.mark.asyncio
async def test_discord_created_invisible_to_agents(make_maker):
    maker = make_maker
    row = await _seed(maker, id="s_ui", agent_id="worker",
                      owner_id="discord-1")
    assert await visible_sessions(maker, "agent-a") == []
    # RULING: host implicit read — the worker produced that transcript
    assert await can_read(maker, "worker", row)
    assert not await can_read(maker, "other-agent", row)
