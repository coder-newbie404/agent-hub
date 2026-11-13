import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import Settings
from app.db import make_engine, make_session_maker
from app.models import Base
from app.delegation import DelegationBroker, chain_offline
from app.registry import AgentRegistry
from app.routes import router as api_router
from app.sessions import SessionManager
from app.tokens import TokenService
from app.ui_events import UiBroker
from app.ws import router as ws_router
from app.ws_ui import router as ws_ui_router

# Keep hub lifecycle logs visible in docker logs.
logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)


def create_app(settings: Settings) -> FastAPI:
    engine = make_engine(settings.sqlite_path)
    maker = make_session_maker(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)  # idempotent schema bootstrap
        tasks = []

        try:
            fresh = await app.state.tokens.ensure_bootstrap(settings.admin_token)
            if fresh:
                logger.info("seeded bootstrap token from ADMIN_TOKEN;"
                            " keep it for the owner UI and mint per-agent"
                            " tokens for agents")
        except Exception:
            # a failed seed must not kill the app: REST stays up and the
            # owner can inspect via /tokens
            logger.exception("bootstrap token seed failed")

        async def sweep_loop():
            while True:
                await asyncio.sleep(settings.heartbeat_interval)
                try:
                    await app.state.registry.sweep(settings.offline_after)
                except Exception:
                    # transient DB hiccups (e.g. 'database is locked') must not
                    # kill the sweep task — offline detection must keep running
                    logger.exception("registry sweep failed")

        tasks.append(asyncio.create_task(sweep_loop()))

        async def lifecycle_loop():
            while True:
                await asyncio.sleep(600)
                try:
                    counts = await app.state.sessions.sweep_lifecycle(
                        settings.delegation_idle_after_s,
                        settings.delegation_archive_after_s)
                    if counts["idled"] or counts["archived"]:
                        logger.info("lifecycle sweep: %s", counts)
                except Exception:
                    # same guard as sweep_loop: a transient DB hiccup must
                    # not kill the lifecycle task
                    logger.exception("lifecycle sweep failed")

        tasks.append(asyncio.create_task(lifecycle_loop()))
        yield
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Close pooled DB connections while this loop is still alive. Without
        # this, aiosqlite worker threads outlive their event loop and garbage
        # collection later force-terminates them (SQLAlchemy logs the pooled
        # connection as leaked; a shielded graceful-close task can linger and
        # stall loop teardown).
        await engine.dispose()

    # interactive docs/schema are unauthenticated and the reverse proxy
    # forwards every path, so they stay off
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.settings = settings
    app.state.registry = AgentRegistry(maker)
    sessions = SessionManager(app.state.registry, maker)
    app.state.sessions = sessions
    # Hub → frontend events: the broker fills the notifier slot the in-process
    # bot used to claim, and fans every event out over /ws/ui.
    broker = UiBroker()
    sessions.notifier = broker
    app.state.ui_broker = broker
    from app.sync import SyncManager
    app.state.sync = SyncManager(maker)
    app.state.sync.on_applied = broker.on_sync_applied
    app.state.tokens = TokenService(maker)
    # Agent→agent delegation: the broker rides the same /ws/agent channel
    # and is resolved when a run finishes via the on_finished hook.
    delegation = DelegationBroker(
        app.state.registry, sessions, maker,
        result_timeout_s=settings.delegation_result_timeout_s,
        max_hop=settings.delegation_max_hop)
    app.state.delegation = delegation
    sessions.on_finished = delegation.on_session_finished
    # The single offline wiring point (moved out of SessionManager): a
    # dropped agent both resolves its in-flight sessions (session truth)
    # and unblocks requesters waiting on its sessions (delegation rounds).
    app.state.registry.on_offline = chain_offline(
        sessions.on_agent_offline, delegation.on_worker_offline)
    app.include_router(api_router)
    app.include_router(ws_router)
    app.include_router(ws_ui_router)
    return app


app = create_app(Settings())
