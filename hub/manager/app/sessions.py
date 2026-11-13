from __future__ import annotations  # `list()` method below shadows the builtin in later annotations
import asyncio
import json
import logging
from datetime import timedelta
from typing import Protocol

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import MessageRow, SessionRow, utcnow
from app.protocol import (Envelope, SESSION_CANCEL, SESSION_CREATE,
                          SESSION_FAILED, SESSION_MESSAGE, SESSION_WAITING,
                          new_id)

CREATED = "created"
RUNNING = "running"
WAITING = "waiting"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
IDLE = "idle"

logger = logging.getLogger(__name__)

CANCEL_ACK_TIMEOUT = 5.0  # session.cancel ack deadline; one retry (failsafe §4)


class SessionError(Exception):
    pass


class NotFound(SessionError):
    pass


class AgentOffline(SessionError):
    pass


class RunInProgress(SessionError):
    pass


def _hub_status(raw: str | None, omp: str | None) -> str:
    """Adapter vocabulary → hub lifecycle vocabulary. Snapshot/sync events
    speak adapter ("idle"/"running"/"failed"); the hub renders its own
    (waiting/completed/failed/...). `idle` means no run in flight:
    completed when the session ever ran (omp id known), else still waiting.
    Anything already in hub vocabulary passes through."""
    if raw == "running":
        return RUNNING
    if raw == "failed":
        return FAILED
    if raw == "idle":
        return COMPLETED if omp else WAITING
    return raw or WAITING


async def upsert_projected(maker: async_sessionmaker[AsyncSession],
                           agent_id: str, local_id: str, *, status: str,
                           workspace: str | None = None,
                           title: str | None = None,
                           omp: str | None = None) -> SessionRow:
    """Import-or-update the hub's projection row for a locally-owned
    session. Keyed by (agent_id, hub id OR omp_session_id) so both address
    styles resolve to one row. `status` may be adapter or hub vocabulary;
    it is normalized here."""
    status = _hub_status(status, omp)
    async with maker() as s:
        row = (await s.execute(
            select(SessionRow).where(
                SessionRow.agent_id == agent_id,
                (SessionRow.id == local_id)
                | (SessionRow.omp_session_id == local_id))
        )).scalars().first()
        if row is None:
            row = SessionRow(id=new_id("sess"), agent_id=agent_id,
                             owner_id="imported", status=status,
                             omp_session_id=local_id, workspace=workspace,
                             title=title)
            s.add(row)
            await s.commit()
            logger.info("imported local session %s of agent %s",
                        local_id, agent_id)
            return row
        values: dict = {"updated_at": utcnow()}
        # IDLE is a legal hub state (the lifecycle sweeper writes it), so a
        # projection touch must not overwrite it; rows still moving
        # (running/waiting) take the incoming status, and an explicit
        # RUNNING/FAILED report always applies — RUNNING is also how a
        # pre-sweeper raw "idle" artifact self-heals, FAILED covers a run
        # whose whole running window the hub missed (restart mid-run)
        if status and (row.status in (RUNNING, WAITING)
                       or status in (RUNNING, FAILED)):
            values["status"] = status
        if workspace:
            values["workspace"] = workspace
        if title:
            values["title"] = title
        # every projection touch is a liveness signal: the hub picker sorts
        # recent-first, so re-asserting a session must bump it even when no
        # field changed
        await s.execute(update(SessionRow)
                        .where(SessionRow.id == row.id).values(**values))
        await s.commit()
        row.status = values.get("status", row.status)
        row.updated_at = values["updated_at"]
        return row


async def resolve_local_row(maker: async_sessionmaker[AsyncSession],
                            agent_id: str, local_id: str) -> SessionRow | None:
    """The agent's address-space mapping (hub id or omp session id →
    row), shared by SessionManager.resolve_local and the sync responder."""
    async with maker() as s:
        return (await s.execute(
            select(SessionRow).where(
                SessionRow.agent_id == agent_id,
                (SessionRow.id == local_id)
                | (SessionRow.omp_session_id == local_id))
        )).scalars().first()


class SessionNotifier(Protocol):
    async def on_message(self, session_id: str, text: str,
                         message_id: int) -> None: ...
    async def on_delta(self, session_id: str, draft: str) -> None: ...
    async def on_activity(self, session_id: str, label: str | None) -> None: ...
    async def on_completed(self, session_id: str) -> None: ...
    async def on_failed(self, session_id: str, reason: str) -> None: ...
    async def on_agent_disconnected(self, agent_id: str) -> None: ...


def _label_from_activity(payload: dict) -> str | None:
    """Render a session.activity payload as a short human label; None means
    the previous activity ended (tool finished) and the UI falls back to a
    generic 'working' state."""
    kind = payload.get("kind")
    if kind == "think":
        return "thinking"
    if kind == "tool":
        if payload.get("state") != "start":
            return None
        tool = payload.get("tool", "tool")
        detail = (payload.get("detail") or "").strip()
        return f"{tool}: {detail}" if detail else tool
    return payload.get("label") or None


class SessionManager:
    def __init__(self, registry, session_maker: async_sessionmaker[AsyncSession],
                 notifier: SessionNotifier | None = None):
        self.registry = registry
        self._maker = session_maker
        self.notifier = notifier
        # Delegation relay hook: fired (as a task) when a run finishes, so
        # the broker can resolve pending A→B rounds without blocking the
        # receive loop. Wired in main.py.
        self.on_finished = None
        self.inflight: dict[str, str] = {}
        self._drafts: dict[str, str] = {}
        # Label of what the agent is currently doing per session, e.g.
        # "bash: echo hi" — composed from session.activity payloads.
        self.activity: dict[str, str] = {}
        self._notify_tasks: set[asyncio.Task] = set()

    async def on_agent_offline(self, agent_id: str,
                               reason: str = "agent offline") -> None:
        # A mid-run agent disconnect must resolve its in-flight sessions:
        # legacy agents fail them (hub owns truth); locally-owning agents
        # keep their reported state until a snapshot resolves it. Wired
        # into registry.on_offline in main.py, chained with the delegation
        # broker's on_worker_offline.
        if self.registry.is_projection(agent_id):
            # projection mode: never fabricate status — the local runtime
            # owns truth; a reconnect snapshot resolves these runs
            for sid in list(self.inflight):
                try:
                    row = await self.get(sid)
                except NotFound:
                    self.inflight.pop(sid, None)
                    continue
                if row.agent_id == agent_id:
                    self._finish(sid)
            if self.notifier:
                self._notify(self.notifier.on_agent_disconnected, agent_id)
            return
        await self.fail_inflight_for_agent(agent_id, reason)

    async def create(self, agent_id: str, owner_id: str | None,
                     workspace: str | None, title: str | None = None,
                     creator_agent_id: str | None = None,
                     description: str = "") -> SessionRow:
        async with self._maker() as s:
            row = SessionRow(id=new_id("sess"), agent_id=agent_id,
                             owner_id=owner_id, status=CREATED,
                             workspace=workspace, title=title,
                             creator_agent_id=creator_agent_id,
                             description=description)
            s.add(row)
            await s.commit()
            return row

    async def _save_status(self, session_id: str, status: str,
                           omp_session_id: str | None = None) -> None:
        async with self._maker() as s:
            values: dict = {"status": status}
            if omp_session_id is not None:
                values["omp_session_id"] = omp_session_id
            await s.execute(update(SessionRow).where(SessionRow.id == session_id)
                            .values(**values))
            await s.commit()

    async def touch(self, session_id: str) -> None:
        async with self._maker() as s:
            await s.execute(update(SessionRow)
                            .where(SessionRow.id == session_id)
                            .values(last_used_at=utcnow()))
            await s.commit()

    async def open_session(self, agent_id: str, owner_id: str,
                           workspace: str | None,
                           title: str | None = None,
                           creator_agent_id: str | None = None,
                           description: str = "") -> SessionRow:
        row = await self.create(agent_id, owner_id, workspace, title,
                                creator_agent_id=creator_agent_id,
                                description=description)
        conn = self.registry.get(agent_id)
        if conn is None:
            await self._save_status(row.id, FAILED)
            raise AgentOffline(f"agent {agent_id} is offline")
        env = Envelope(type=SESSION_CREATE, request_id=new_id("req"),
                       session_id=row.id,
                       payload={"workspace": workspace or "", "created_by": owner_id})
        try:
            resp = await conn.call(env)
        except (TimeoutError, ConnectionError):
            await self._save_status(row.id, FAILED)
            raise AgentOffline(f"agent {agent_id} unreachable") from None
        if resp.type == SESSION_FAILED:
            reason = resp.payload.get("reason", "create failed")
            await self._save_status(row.id, FAILED)
            raise SessionError(reason)
        await self._save_status(row.id, WAITING)
        row.status = WAITING
        return row

    async def attach_thread(self, session_id: str, thread_id: int) -> None:
        async with self._maker() as s:
            await s.execute(update(SessionRow).where(SessionRow.id == session_id)
                            .values(thread_id=thread_id))
            await s.commit()

    async def rename(self, session_id: str, title: str) -> SessionRow:
        async with self._maker() as s:
            await s.execute(update(SessionRow).where(SessionRow.id == session_id)
                            .values(title=title))
            await s.commit()
        return await self.get(session_id)

    async def set_archived(self, session_id: str, archived: bool) -> None:
        async with self._maker() as s:
            await s.execute(update(SessionRow).where(SessionRow.id == session_id)
                            .values(archived=archived))
            await s.commit()

    async def delete(self, session_id: str) -> None:
        """Remove a session and its transcript. Refuses while a run is in
        flight; the Discord thread itself is left in place."""
        if session_id in self.inflight:
            raise RunInProgress("stop the run before deleting this session")
        async with self._maker() as s:
            row = await s.get(SessionRow, session_id)
            if row is None:
                raise NotFound(f"unknown session {session_id}")
            # messages carry no ON DELETE CASCADE (migration c97a618b644a)
            await s.execute(delete(MessageRow).where(
                MessageRow.session_id == session_id))
            await s.delete(row)
            await s.commit()

    async def message_count(self, session_id: str) -> int:
        async with self._maker() as s:
            return await s.scalar(select(func.count()).select_from(MessageRow)
                                  .where(MessageRow.session_id == session_id)) or 0

    async def get(self, session_id: str) -> SessionRow:
        async with self._maker() as s:
            row = await s.get(SessionRow, session_id)
            if row is None:
                raise NotFound(f"unknown session {session_id}")
            return row

    async def by_thread(self, thread_id: int) -> SessionRow | None:
        async with self._maker() as s:
            row = (await s.execute(select(SessionRow)
                                   .where(SessionRow.thread_id == thread_id))).scalar_one_or_none()
            return row

    async def list(self) -> list[SessionRow]:
        async with self._maker() as s:
            return (await s.execute(select(SessionRow)
                                    .order_by(SessionRow.created_at))).scalars().all()

    async def user_message(self, session_id: str, text: str,
                           request_id: str | None = None) -> str:
        row = await self.get(session_id)
        conn = self.registry.get(row.agent_id)
        if conn is None:
            raise AgentOffline(f"agent {row.agent_id} is offline")
        if session_id in self.inflight:
            raise RunInProgress("a request is already in flight for this session")
        req_id = request_id or new_id("req")
        self.inflight[session_id] = req_id
        await self._save_status(session_id, RUNNING)
        await self.touch(session_id)
        async with self._maker() as s:
            s.add(MessageRow(session_id=session_id, request_id=req_id,
                             role="user", content=text, posted_at=utcnow()))
            await s.commit()
        try:
            await conn.send(Envelope(type=SESSION_MESSAGE, request_id=req_id,
                                     session_id=session_id, payload={"text": text}))
        except Exception:
            self.inflight.pop(session_id, None)
            await self._save_status(session_id, FAILED)
            await self.registry.mark_offline(row.agent_id)
            raise AgentOffline(f"agent {row.agent_id} dropped") from None
        return req_id

    async def cancel(self, session_id: str) -> bool:
        return await self._cancel_session(session_id, CANCEL_ACK_TIMEOUT)

    async def _cancel_session(self, session_id: str,
                              ack_timeout: float) -> bool:
        if session_id not in self.inflight:
            return False
        row = await self.get(session_id)
        conn = self.registry.get(row.agent_id)
        if conn is None:
            return True
        if not self.registry.is_projection(row.agent_id):
            # legacy adapter: fire-and-forget, run outcome arrives as an event
            try:
                await conn.send(Envelope(type=SESSION_CANCEL,
                                         session_id=session_id))
            except Exception:
                pass
            return True
        # locally-owning adapter: acked cancel, one retry (failsafe plan §4);
        # an unacknowledged cancel never fabricates state — the eventual
        # session.failed/session.completed event resolves the run card
        for attempt in (1, 2):
            env = Envelope(type=SESSION_CANCEL, request_id=new_id("req"),
                           session_id=session_id)
            try:
                ack = await conn.call(env, timeout=ack_timeout)
                logger.info("cancel for %s acked: %s", session_id,
                            ack.payload.get("state", "accepted"))
                return True
            except TimeoutError:
                logger.warning("cancel for %s unacknowledged (attempt %d)",
                               session_id, attempt)
            except ConnectionError:
                break
        return True

    async def on_waiting(self, session_id: str, state: str) -> None:
        if state == "running":
            await self._save_status(session_id, RUNNING)

    async def on_session_delta(self, session_id: str, text: str | None,
                               reset: bool) -> None:
        """Streaming preview text; the final agent.message stays the
        authority for both the DB and the thread."""
        if reset or session_id not in self._drafts:
            self._drafts[session_id] = ""
        if text:
            self._drafts[session_id] += text
        if self.notifier:
            self._notify(self.notifier.on_delta, session_id,
                         self._drafts[session_id])

    async def on_session_activity(self, session_id: str,
                                  payload: dict) -> None:
        label = _label_from_activity(payload)
        if label is None:
            self.activity.pop(session_id, None)
        else:
            self.activity[session_id] = label
        if self.notifier:
            self._notify(self.notifier.on_activity, session_id, label)

    def _finish(self, session_id: str) -> None:
        self.inflight.pop(session_id, None)
        self._drafts.pop(session_id, None)
        self.activity.pop(session_id, None)

    async def on_agent_message(self, session_id: str, text: str,
                               request_id: str | None = None) -> int:
        async with self._maker() as s:
            row = MessageRow(session_id=session_id, request_id=request_id,
                             role="assistant", content=text)
            s.add(row)
            await s.commit()
            mid = row.id
        if self.notifier:
            # Full reply goes to the thread (chunked); it is also in the
            # messages table and in omp's session files.
            self._notify(self.notifier.on_message, session_id, text, mid)
        return mid

    async def messages(self, session_id: str) -> list[MessageRow]:
        async with self._maker() as s:
            return (await s.execute(
                select(MessageRow).where(MessageRow.session_id == session_id)
                .order_by(MessageRow.id))).scalars().all()

    async def missed_messages(self, session_id: str) -> list[MessageRow]:
        async with self._maker() as s:
            return (await s.execute(
                select(MessageRow)
                .where(MessageRow.session_id == session_id,
                       MessageRow.posted_at.is_(None))
                .order_by(MessageRow.id))).scalars().all()

    async def missed_session_ids(self) -> list[str]:
        async with self._maker() as s:
            rows = (await s.execute(
                select(MessageRow.session_id)
                .where(MessageRow.posted_at.is_(None))
                .group_by(MessageRow.session_id))).all()
        return [r[0] for r in rows]

    async def mark_posted(self, ids: list[int]) -> None:
        if not ids:
            return
        async with self._maker() as s:
            await s.execute(update(MessageRow)
                            .where(MessageRow.id.in_(ids))
                            .values(posted_at=utcnow()))
            await s.commit()

    async def resolve_local(self, agent_id: str,
                            local_id: str) -> SessionRow | None:
        """A projection agent addresses sessions by hub id (its own
        SESSION_CREATE sid) or by omp session id (imported rows)."""
        return await resolve_local_row(self._maker, agent_id, local_id)

    async def on_completed(self, session_id: str,
                           omp_session_id: str | None = None,
                           payload: dict | None = None) -> None:
        self._finish(session_id)
        await self._save_status(session_id, COMPLETED, omp_session_id)
        await self.touch(session_id)
        if self.notifier:
            self._notify(self.notifier.on_completed, session_id)
        if self.on_finished:
            self._notify(self.on_finished, session_id, "completed",
                         payload or {})

    async def on_failed(self, session_id: str, reason: str) -> None:
        self._finish(session_id)
        status = CANCELLED if reason == "cancelled" else FAILED
        await self._save_status(session_id, status)
        if self.notifier:
            self._notify(self.notifier.on_failed, session_id, reason)
        if self.on_finished:
            self._notify(self.on_finished, session_id,
                         "cancelled" if reason == "cancelled" else "failed",
                         {"reason": reason})

    def _notify(self, method, *args) -> None:
        """Dispatch notifier work as a background task: slow Discord sends
        must not block the ws receive loop or a connection unwind."""
        task = asyncio.create_task(method(*args))
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_done)

    def _notify_done(self, task: asyncio.Task) -> None:
        self._notify_tasks.discard(task)
        if not task.cancelled() and task.exception():
            logger.warning("session notifier failed",
                           exc_info=task.exception())

    async def fail_inflight_for_agent(self, agent_id: str,
                                      reason: str = "agent offline") -> None:
        """Fail every in-flight session of `agent_id`; legacy agents only
        (the hub owns their truth). Projection-mode agents take the
        snapshot/disconnect path in on_agent_offline instead."""
        for sid in list(self.inflight):
            try:
                row = await self.get(sid)
            except NotFound:
                self.inflight.pop(sid, None)
                continue
            if row.agent_id != agent_id:
                continue
            self._finish(sid)
            await self._save_status(sid, FAILED)
            if self.notifier:
                self._notify(self.notifier.on_failed, sid, reason)

    async def reconcile_snapshot(self, agent_id: str, rows: list[dict],
                                 done: bool) -> None:
        """Apply a locally-owning agent's snapshot: upsert via
        upsert_projected (hub id OR omp_session_id), import unknown
        sessions; with `done`, hub rows the agent no longer reports as
        active are resolved as lost."""
        seen: set[str] = set()
        for row in rows:
            local_id = str(row.get("local_id") or "").strip()
            if not local_id:
                continue
            seen.add(local_id)
            existing = await upsert_projected(
                self._maker, agent_id, local_id,
                status=row.get("status") or "idle",
                workspace=row.get("workspace"), title=row.get("title"),
                omp=row.get("omp"))
            if existing.id in self.inflight and existing.status != RUNNING:
                self.inflight.pop(existing.id, None)
        if not done:
            return
        async with self._maker() as s:
            active = (await s.execute(
                select(SessionRow).where(
                    SessionRow.agent_id == agent_id,
                    SessionRow.status.in_([RUNNING, WAITING])))
            ).scalars().all()
        for r in active:
            if r.id in seen or (r.omp_session_id or "") in seen:
                continue
            self._finish(r.id)
            await self._save_status(r.id, FAILED)
            if self.notifier:
                self._notify(self.notifier.on_failed, r.id, "missing locally")

    async def recover_interrupted(self) -> list[str]:
        async with self._maker() as s:
            rows = (await s.execute(select(SessionRow).where(
                SessionRow.status.in_([RUNNING, WAITING])))).scalars().all()
            ids = [r.id for r in rows]
            if ids:
                await s.execute(update(SessionRow).where(SessionRow.id.in_(ids))
                                .values(status=FAILED))
                await s.commit()
        return ids

    async def sweep_lifecycle(self, idle_after_s: int,
                              archive_after_s: int) -> dict:
        """Hub-side lifecycle backstop (spec §9): quiet completed/waiting
        sessions go idle, long-unused sessions get archived. Never idles
        running/failed/cancelled rows or in-flight runs, and never touches
        rows that were never used (last_used_at IS NULL)."""
        now = utcnow()
        async with self._maker() as s:
            idle_cut = now - timedelta(seconds=idle_after_s)
            rows = (await s.execute(select(SessionRow).where(
                SessionRow.status.in_([COMPLETED, WAITING]),
                SessionRow.last_used_at.is_not(None),
                SessionRow.last_used_at < idle_cut))).scalars().all()
            idled = [r.id for r in rows if r.id not in self.inflight]
            if idled:
                await s.execute(update(SessionRow)
                                .where(SessionRow.id.in_(idled))
                                .values(status=IDLE))
            arch_cut = now - timedelta(seconds=archive_after_s)
            rows = (await s.execute(select(SessionRow).where(
                SessionRow.status.not_in([FAILED, CANCELLED]),
                SessionRow.last_used_at.is_not(None),
                SessionRow.last_used_at < arch_cut,
                SessionRow.archived.is_(False)))).scalars().all()
            to_archive = [r.id for r in rows]
            if to_archive:
                await s.execute(update(SessionRow)
                                .where(SessionRow.id.in_(to_archive))
                                .values(archived=True))
            await s.commit()
        return {"idled": len(idled), "archived": len(to_archive)}
