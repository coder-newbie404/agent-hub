from sqlalchemy import event
from sqlalchemy.ext.asyncio import (AsyncEngine, async_sessionmaker,
                                    create_async_engine)
from sqlalchemy.pool import StaticPool
from app.models import Base  # noqa: F401  (re-export convenience)


def make_engine(path: str) -> AsyncEngine:
    kwargs = {"poolclass": StaticPool} if path == ":memory:" else {}
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", **kwargs)
    if path != ":memory:":
        @event.listens_for(engine.sync_engine, "connect")
        def _wal(dbapi_conn, _record):
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
    return engine


def make_session_maker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
