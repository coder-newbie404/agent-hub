import asyncio
import json
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AgentRow, utcnow
from app.protocol import Envelope


class AgentConnection:
    def __init__(self, agent_id: str, ws):
        self.agent_id = agent_id
        self.closed = False
        self._ws = ws
        self._pending: dict[tuple[str | None, str | None], asyncio.Future] = {}

    async def send(self, env: Envelope) -> None:
        if self.closed:
            raise ConnectionError("connection closed")
        await self._ws.send_text(env.model_dump_json())

    async def call(self, env: Envelope, timeout: float = 30.0) -> Envelope:
        if self.closed:
            raise ConnectionError("connection closed")
        fut = asyncio.get_running_loop().create_future()
        self._pending[(env.session_id, env.request_id)] = fut
        try:
            await self.send(env)
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop((env.session_id, env.request_id), None)

    def resolve(self, env: Envelope) -> bool:
        fut = self._pending.pop((env.session_id, env.request_id), None)
        if fut and not fut.done():
            fut.set_result(env)
            return True
        return False

    async def close(self) -> None:
        self.closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("agent disconnected"))
        self._pending.clear()
        try:
            await self._ws.close()
        except Exception:
            pass


class AgentRegistry:
    def __init__(self, session_maker: async_sessionmaker[AsyncSession]):
        self._maker = session_maker
        self._conns: dict[str, AgentConnection] = {}
        self._last_seen: dict[str, float] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        # Set in main.py (chain: session truth + delegation rounds); called
        # with the agent_id whenever the agent goes offline. A hook keeps
        # this module free of session imports.
        self.on_offline: Callable[[str], Awaitable[None]] | None = None
        # Agents that own their sessions locally (protocol v1
        # `owns_sessions` capability); the hub then runs in projection mode
        # for them. Survives disconnects — the flag is per agent_id.
        self._projection: set[str] = set()

    def set_projection(self, agent_id: str, owns_sessions: bool) -> None:
        if owns_sessions:
            self._projection.add(agent_id)
        else:
            self._projection.discard(agent_id)

    def is_projection(self, agent_id: str) -> bool:
        return agent_id in self._projection

    def projection_agents(self) -> list[str]:
        """Agent ids running in locally-owning (projection) mode."""
        return list(self._projection)

    def get(self, agent_id: str) -> AgentConnection | None:
        return self._conns.get(agent_id)

    def online(self, agent_id: str) -> bool:
        return agent_id in self._conns

    async def register(self, agent_id: str, name: str,
                       capabilities: list[str], ws,
                       description: str = "", tools: list | None = None,
                       environment: dict | None = None) -> AgentConnection:
        async with self._locks[agent_id]:
            stale = self._conns.get(agent_id)
            if stale:
                await stale.close()
            conn = AgentConnection(agent_id, ws)
            self._conns[agent_id] = conn
            self._last_seen[agent_id] = time.monotonic()
            async with self._maker() as s:
                row = await s.get(AgentRow, agent_id)
                if row is None:
                    row = AgentRow(id=agent_id, name=name)
                    s.add(row)
                row.name = name
                row.status = "online"
                row.capabilities = json.dumps(capabilities)
                row.description = description
                row.tools = json.dumps(tools or [])
                row.environment = json.dumps(environment or {})
                row.last_seen = utcnow()
                await s.commit()
            return conn

    async def touch(self, agent_id: str) -> None:
        self._last_seen[agent_id] = time.monotonic()
        async with self._maker() as s:
            row = await s.get(AgentRow, agent_id)
            if row:
                row.last_seen = utcnow()
                await s.commit()

    async def mark_offline(self, agent_id: str) -> None:
        async with self._locks[agent_id]:
            await self._take_offline_locked(agent_id, self._conns.get(agent_id))

    async def mark_offline_if_current(self, conn: AgentConnection) -> None:
        """Like mark_offline, but only if `conn` is still the agent's
        registered connection: a stale receive loop's finally must not
        close a newer connection that re-registered in the meantime."""
        agent_id = conn.agent_id
        async with self._locks[agent_id]:
            if self._conns.get(agent_id) is not conn:
                return
            await self._take_offline_locked(agent_id, conn)

    async def _take_offline_locked(self, agent_id: str,
                                   conn: AgentConnection | None) -> None:
        if conn:
            self._conns.pop(agent_id, None)
            await conn.close()
        self._last_seen.pop(agent_id, None)
        async with self._maker() as s:
            row = await s.get(AgentRow, agent_id)
            if row:
                row.status = "offline"
                await s.commit()
        if self.on_offline is not None:
            await self.on_offline(agent_id)

    async def sweep(self, offline_after: int) -> list[str]:
        now = time.monotonic()
        stale = [aid for aid, seen in self._last_seen.items()
                 if aid in self._conns and now - seen > offline_after]
        for aid in stale:
            await self.mark_offline(aid)
        return stale

    async def list(self) -> list[AgentRow]:
        async with self._maker() as s:
            return (await s.execute(select(AgentRow).order_by(AgentRow.id))).scalars().all()

    async def capabilities(self, agent_id: str) -> set[str]:
        """Declared capabilities as a set; empty when unknown or malformed.
        The bot uses this to decide which controls to render (roadmap §11)."""
        async with self._maker() as s:
            row = await s.get(AgentRow, agent_id)
        if row is None:
            return set()
        try:
            return set(json.loads(row.capabilities or "[]"))
        except (TypeError, ValueError):
            return set()
