import json

from sqlalchemy import select

from app.db import make_engine, make_session_maker
from app.models import AgentRow, Base, MessageRow, SessionRow, utcnow


async def test_create_and_query_rows():
    engine = make_engine(":memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = make_session_maker(engine)
    async with maker() as s:
        s.add(AgentRow(id="omp-local", name="OMP Local", status="online",
                       capabilities=json.dumps(["chat"]), last_seen=utcnow()))
        s.add(SessionRow(id="sess_a", agent_id="omp-local", owner_id="1",
                         status="created", workspace="/tmp"))
        s.add(MessageRow(session_id="sess_a", request_id="req_1",
                         role="user", content="hello"))
        await s.commit()
        rows = (await s.execute(select(SessionRow))).scalars().all()
        assert len(rows) == 1 and rows[0].thread_id is None
        msgs = (await s.execute(select(MessageRow))).scalars().all()
        assert msgs[0].role == "user"


async def test_datetimes_roundtrip_naive():
    engine = make_engine(":memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = make_session_maker(engine)
    async with maker() as s:
        written = utcnow()
        s.add(AgentRow(id="omp-local", name="OMP Local", last_seen=written))
        await s.commit()
    async with maker() as s2:  # fresh session: force a genuine DB read
        fetched = (await s2.execute(select(AgentRow))).scalar_one()
        assert fetched.last_seen.tzinfo is None
        assert fetched.last_seen == written


async def test_message_posted_at_roundtrip():
    engine = make_engine(":memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = make_session_maker(engine)
    async with maker() as s:
        s.add(SessionRow(id="sess_a", agent_id="omp-local", owner_id="1"))
        s.add(MessageRow(session_id="sess_a", role="assistant",
                         content="hi", request_id="req_1"))
        await s.commit()
        row = (await s.execute(select(MessageRow))).scalar_one()
        assert row.posted_at is None  # never rendered in Discord yet
        from app.models import SyncStateRow
        s.add(SyncStateRow(agent_id="omp-local", instance="j-1", cursor=7))
        await s.commit()
        st = (await s.execute(select(SyncStateRow))).scalar_one()
        assert (st.instance, st.cursor) == ("j-1", 7)
