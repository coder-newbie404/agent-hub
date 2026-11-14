"""Hub bridge: translates hub envelopes into SessionRuntime calls.

The runtime (runtime.py) is the source of truth — sessions, runs, journal,
crash recovery. This module adds only hub-facing concerns: workspace
validation, snapshot emission on (re)connect, cancel acknowledgements, and
the register-time owns_sessions capability (client.py).
"""
import asyncio
import logging
import pathlib

from agent_adapter.protocol import (Envelope, SESSION_CANCEL,
                                    SESSION_CANCEL_ACK, SESSION_CREATE,
                                    SESSION_FAILED, SESSION_MESSAGE,
                                    SESSION_WAITING, SESSIONS_SNAPSHOT,
                                    SYNC_ACK, SYNC_MESSAGES, SYNC_NEED,
                                    SYNC_NOW, SYNC_PUSH)
from agent_adapter.runtime import SessionRuntime

logger = logging.getLogger(__name__)

SNAPSHOT_BATCH = 200  # spec: batches ≤200 sessions, final batch sets done


class AdapterAgent:
    def __init__(self, settings, send):
        self.settings = settings
        self._send = send
        self.runtime = SessionRuntime(settings, send)
        self._started = False
        self._sync_cursor = 0
        self._sync_cursor_ready = False
        self._sync_wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()

    async def on_connect(self) -> None:
        """First connect: journal replay + crash sweep (idempotent). Every
        connect: push the session snapshot so the hub reconciles truth."""
        if not self._started:
            await self.runtime.start()
            self._started = True
        await self.send_snapshot()

    async def send_snapshot(self) -> None:
        rows = self.runtime.snapshot_rows()
        for start in range(0, len(rows), SNAPSHOT_BATCH) or [0]:
            await self._send(Envelope(
                type=SESSIONS_SNAPSHOT,
                payload={"sessions": rows[start:start + SNAPSHOT_BATCH],
                         "done": start + SNAPSHOT_BATCH >= len(rows)}))

    def _workspace_ok(self, ws: str) -> bool:
        p = pathlib.Path(ws).expanduser()
        if not p.is_dir():
            return False
        if self.settings.allowed_root:
            root = pathlib.Path(self.settings.allowed_root).expanduser().resolve()
            if not p.resolve().is_relative_to(root):
                return False
        return True

    async def handle(self, env: Envelope) -> None:
        sid = env.session_id or ""
        if env.type == SESSION_CREATE:
            ws = env.payload.get("workspace") or self.settings.default_workspace
            if not self._workspace_ok(ws):
                await self._send(Envelope(
                    type=SESSION_FAILED, request_id=env.request_id,
                    session_id=sid,
                    payload={"reason": f"bad workspace: {ws}"}))
                return
            self.runtime.upsert_session(sid, ws)
            await self._send(Envelope(type=SESSION_WAITING,
                                      request_id=env.request_id,
                                      session_id=sid,
                                      payload={"state": "created"}))
        elif env.type == SESSION_MESSAGE:
            outcome = self.runtime.begin_run(sid, env.payload.get("text", ""),
                                             env.request_id,
                                             env.payload.get("env"))
            if outcome == "busy":
                await self._send(Envelope(
                    type=SESSION_FAILED, request_id=env.request_id,
                    session_id=sid, payload={"reason": "busy"}))
            # duplicate: silent ack — the original run's events already
            # resolve the hub's request; respawning would run omp twice.
        elif env.type == SYNC_ACK:
            await self._handle_sync_ack(env.payload)
        elif env.type == SYNC_MESSAGES:
            self._handle_sync_messages(env.payload)
        elif env.type == SYNC_NOW:
            self.nudge()
            # drain immediately; the nudge also wakes sync_loop for the
            # next journal tail
            await self.push_pending()
        elif env.type == SESSION_CANCEL:
            # ack mirrors the hub's conn.call matching: same session_id +
            # request_id; the eventual session.failed/completed event
            # remains the authority on the actual outcome.
            state = self.runtime.cancel(sid)
            await self._send(Envelope(type=SESSION_CANCEL_ACK,
                                      request_id=env.request_id,
                                      session_id=sid,
                                      payload={"state": state}))

    # -- journal → hub sync ----------------------------------------------

    def nudge(self) -> None:
        self._sync_wake.set()

    async def sync_loop(self) -> None:
        """Periodic + nudged journal-tail push. One batch chain per tick;
        an unacked (offline) cursor simply resends until the hub's
        cumulative ack catches up."""
        while True:
            self._idle.set()
            try:
                await asyncio.wait_for(self._sync_wake.wait(),
                                       self.settings.sync_interval)
            except (TimeoutError, asyncio.TimeoutError):
                pass
            self._sync_wake.clear()
            self._idle.clear()
            try:
                self.runtime.refresh_titles()
                await self.push_pending()
            except Exception:
                logger.warning("sync push failed", exc_info=True)
            finally:
                self._idle.set()

    async def push_pending(self) -> None:
        if not self._sync_cursor_ready:
            return  # hub hasn't told us its cursor yet (first ack pending)
        while True:
            events, end = self.runtime.journal.read_from(
                self._sync_cursor, self.settings.sync_batch)
            if not events:
                return
            await self._send(Envelope(
                type=SYNC_PUSH,
                payload={"instance": self.runtime.instance,
                         "cursor_start": self._sync_cursor,
                         "cursor_end": end, "events": events}))
            self._sync_cursor = end  # optimistic; a gap ack rewinds us
            if len(events) < self.settings.sync_batch:
                return

    async def _handle_sync_ack(self, payload: dict) -> None:
        if payload.get("instance") != self.runtime.instance:
            # hub state is stale/absent for this journal (fresh DB, first
            # deploy): its cursor is meaningless, but it is still an ack —
            # rewind so the next push re-establishes the instance pair
            logger.info("hub state stale/absent for journal instance %r; "
                        "rewinding to 0", payload.get("instance"))
            self._sync_cursor = 0
            self._sync_cursor_ready = True
            return
        self._sync_cursor = int(payload.get("cursor", 0))
        self._sync_cursor_ready = True
        for entry in payload.get("catalog", []):
            lid = entry.get("omp_session_id")
            # entries without an omp id are hub-internal rows (a hub-created
            # session whose create never landed): never adopt them as local
            # sessions — that is how ghost ids leak into the journal
            if not lid:
                continue
            # the hub reports completed Discord sessions by omp id while the
            # journal keys them by the hub-shared local id: resolve through
            # the omp index before treating the id as unknown
            local = lid if lid in self.runtime._state \
                else self.runtime.local_for_native(lid)
            if local is None:
                # truly unknown: adopt so the mirror pull has a target
                self.runtime.upsert_session(lid)
                local = lid
            if self.runtime.local_message_count(local) \
                    < int(entry.get("message_count", 0)):
                await self._send(Envelope(
                    type=SYNC_NEED,
                    payload={"omp_session_id": local,
                             "after_message_id":
                                 self.runtime.hub_last_msg(local)}))

    def _handle_sync_messages(self, payload: dict) -> None:
        lid = payload.get("omp_session_id") or ""
        if lid:
            self.runtime.merge_hub_messages(
                lid, list(payload.get("messages", [])))

    async def stop_work(self) -> None:
        await self.runtime.stop_work()
