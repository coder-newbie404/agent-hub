"""Session-owning runtime: the local source of truth behind the bridge.

Owns the session registry (journal-backed), the omp run lifecycle (spawn,
stream forwarding, cancel), crash recovery, and request-id dedupe. The
bridge (agent.py) only translates envelopes to calls on this module and
forwards emitted envelopes to the hub.
"""
import asyncio
import functools
import json
import logging
import os
import pathlib
import signal
import sqlite3
import uuid
from collections import deque
from datetime import datetime, timezone

from omp_adapter.omp import OmpRunner
from omp_adapter.protocol import (AGENT_MESSAGE, Envelope, SESSION_ACTIVITY,
                                  SESSION_COMPLETED, SESSION_DELTA,
                                  SESSION_FAILED, SESSION_WAITING)

logger = logging.getLogger(__name__)

# Bounded cache of recently accepted session.message request ids; a retried
# request inside this window is acked as already-done, never re-spawned.
DEDUPE_CACHE_SIZE = 512


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_legacy_state(raw: dict) -> dict[str, dict]:
    """Normalize legacy state.json entries. v1 kept {sid: omp_id}; v2 kept
    {sid: {"omp": ..., "ws": ...}}. Legacy plain strings mean omp id only,
    workspace unknown."""
    out: dict[str, dict] = {}
    for sid, entry in raw.items():
        if isinstance(entry, dict):
            out[sid] = {k: entry[k] for k in ("omp", "ws") if entry.get(k)}
        else:
            out[sid] = {"omp": entry}
    return out


class SessionJournal:
    """Append-only JSONL journal: one event object per line, fsync per
    append. Replaces the old whole-file state.json rewrites."""

    def __init__(self, journal_dir: str, legacy_state_file: str | None = None):
        self.dir = pathlib.Path(journal_dir).expanduser()
        self.path = self.dir / "journal.jsonl"
        self.legacy_path = (pathlib.Path(legacy_state_file).expanduser()
                            if legacy_state_file else None)
        self.instance: str = uuid.uuid4().hex

    # -- writes ---------------------------------------------------------

    def append(self, event: dict) -> bool:
        line = json.dumps(event)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            logger.warning("journal append failed (%s); durability lost for "
                           "this event", event.get("kind"), exc_info=True)
            return False
        return True

    def session_upserted(self, local_id: str, omp: str | None,
                         ws: str | None, status: str) -> bool:
        return self.append({"kind": "session_upserted", "local_id": local_id,
                            "omp": omp, "ws": ws, "status": status,
                            "updated_at": _now()})

    def run_started(self, local_id: str, pid: int | None) -> bool:
        return self.append({"kind": "run_started", "local_id": local_id,
                            "pid": pid})

    def run_finished(self, local_id: str, status: str) -> bool:
        return self.append({"kind": "run_finished", "local_id": local_id,
                            "status": status})

    def journal_started(self, instance: str) -> bool:
        return self.append({"kind": "journal_started", "instance": instance})

    def message(self, local_id: str, role: str, content: str, ts: str,
                request_id: str | None, mirrored: bool = False) -> bool:
        ev = {"kind": "message", "local_id": local_id,
              "role": role, "content": content, "ts": ts,
              "request_id": request_id}
        if mirrored:
            ev["mirrored"] = True
        return self.append(ev)

    def titled(self, local_id: str, title: str) -> bool:
        return self.append({"kind": "session_titled", "local_id": local_id,
                            "title": title})

    def hub_cursor(self, local_id: str, last_message_id: int) -> bool:
        return self.append({"kind": "hub_cursor", "local_id": local_id,
                            "last_message_id": last_message_id})

    # -- reads ----------------------------------------------------------

    def replay(self) -> tuple[dict[str, dict], dict[str, dict],
                              dict[str, list]]:
        """Fold the journal into (state, unfinished_runs, transcripts). A
        corrupt final line is truncated away (earlier lines survive); a
        legacy state.json is imported when the journal is absent/empty.
        Adopts the persisted journal instance, or starts one so the hub
        can tell a wiped journal from a live one."""
        state: dict[str, dict] = {}
        unfinished: dict[str, dict] = {}
        messages: dict[str, list] = {}
        raw = self._read_raw()
        if raw is None:
            self._import_legacy(state)
            self.journal_started(self.instance)
            return state, unfinished, messages
        first_bad: int | None = None
        last_good: int | None = None
        seen_instance: str | None = None
        pos = 0
        for chunk in raw.split(b"\n"):
            start, pos = pos, pos + len(chunk) + 1
            if not chunk.strip():
                continue
            try:
                ev = json.loads(chunk)
            except (json.JSONDecodeError, UnicodeDecodeError):
                if first_bad is None:
                    first_bad = start
                logger.warning("corrupt journal line at byte %d; skipping",
                               start)
                continue
            seen = self._apply(ev, state, unfinished, messages)
            if seen is not None:
                seen_instance = seen
            last_good = start
        if first_bad is not None and (last_good is None or last_good < first_bad):
            self._truncate(raw, first_bad)
        if seen_instance:
            self.instance = seen_instance
        else:
            self.journal_started(self.instance)
        return state, unfinished, messages

    def read_from(self, start: int, limit: int) -> tuple[list[dict], int]:
        """Parsed non-blank journal lines [start, start+limit) — the same
        indexing replay() folds, so offsets are stable cursors. The
        returned index never passes events that were not returned: a full
        window stops at the next unread line; a corrupt line stops the
        read there (replay truncates such a tail)."""
        events: list[dict] = []
        idx = 0
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return [], start
        for chunk in raw.split(b"\n"):
            if not chunk.strip():
                continue
            if idx >= start:
                if len(events) >= limit:
                    break
                try:
                    events.append(json.loads(chunk))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    break
            idx += 1
        return events, idx

    def _read_raw(self) -> bytes | None:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None
        return raw if raw.strip() else None

    def _import_legacy(self, state: dict[str, dict]) -> None:
        if self.legacy_path is None or not self.legacy_path.exists():
            return
        try:
            entries = _load_legacy_state(
                json.loads(self.legacy_path.read_text()))
        except (OSError, ValueError):
            logger.warning("legacy state file %s unreadable; skipping import",
                           self.legacy_path, exc_info=True)
            return
        for sid, entry in entries.items():
            omp, ws = entry.get("omp"), entry.get("ws")
            state[sid] = {"omp": omp, "ws": ws, "status": "idle"}
            self.session_upserted(sid, omp, ws, "idle")
        # the old state.json is deliberately left in place

    def _truncate(self, raw: bytes, offset: int) -> None:
        logger.warning("truncating corrupt journal tail at byte %d", offset)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            with open(tmp, "wb") as f:
                f.write(raw[:offset])
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError:
            logger.warning("journal tail truncation failed", exc_info=True)

    @staticmethod
    def _apply(ev: dict, state: dict[str, dict],
               unfinished: dict[str, dict],
               messages: dict[str, list]) -> str | None:
        kind, sid = ev.get("kind"), ev.get("local_id")
        if kind == "journal_started":
            return str(ev.get("instance") or "")
        if not sid:
            return None
        if kind == "session_upserted":
            entry = {"omp": ev.get("omp"), "ws": ev.get("ws"),
                     "status": ev.get("status", "idle")}
            # only hub_cursor/titled events write these; a fresh upsert
            # must not silently drop fields folded from earlier in the
            # journal
            prev = state.get(sid) or {}
            if prev.get("hub_last_msg") is not None:
                entry["hub_last_msg"] = prev["hub_last_msg"]
            if prev.get("title"):
                entry["title"] = prev["title"]
            state[sid] = entry
        elif kind == "session_titled":
            if sid in state and ev.get("title"):
                state[sid]["title"] = ev["title"]
        elif kind == "run_started":
            unfinished[sid] = {"pid": ev.get("pid")}
        elif kind == "run_finished":
            unfinished.pop(sid, None)
            if sid in state:
                state[sid]["status"] = ev.get("status", "failed")
        elif kind == "message":
            messages.setdefault(sid, []).append(
                {"role": ev.get("role"), "content": ev.get("content"),
                 "ts": ev.get("ts"), "request_id": ev.get("request_id")})
        elif kind == "hub_cursor":
            if sid in state:
                state[sid]["hub_last_msg"] = ev.get("last_message_id", 0)
        return None


class SessionRuntime:
    """Owns sessions and their runs. `on_event` is an async callable taking
    an Envelope — the bridge forwards emitted envelopes to the hub."""

    def __init__(self, settings, on_event):
        self.settings = settings
        self.on_event = on_event
        self.journal = SessionJournal(settings.journal_dir,
                                      legacy_state_file=settings.state_file)
        self._state: dict[str, dict] = {}
        self._active: dict[str, asyncio.Task] = {}
        self._runners: dict[str, OmpRunner] = {}
        self._seen_requests: deque[str] = deque(maxlen=DEDUPE_CACHE_SIZE)
        self._messages: dict[str, list] = {}
        self._started = False

    @property
    def instance(self) -> str:
        return self.journal.instance

    # -- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        """Replay the journal, then sweep runs left unfinished by a previous
        process: kill leftover process groups, mark everything unfinished as
        failed. Idempotent."""
        if self._started:
            return
        self._started = True
        state, unfinished, messages = self.journal.replay()
        self._state = state
        self._messages = messages
        for sid, info in unfinished.items():
            self._recover_run(sid, info.get("pid"))

    def _recover_run(self, sid: str, pid) -> None:
        # pgid == omp child pid (start_new_session). Guard against pid 0/1
        # and non-ints: killpg(0) would target our own group.
        live = False
        if isinstance(pid, int) and pid > 1:
            try:
                os.killpg(pid, 0)
                live = True
            except (ProcessLookupError, PermissionError, OSError):
                live = False
        if live:
            try:
                os.killpg(pid, signal.SIGKILL)
                logger.warning("startup sweep: killed leftover run group for "
                               "%s (pid %s)", sid, pid)
            except OSError:
                pass
        else:
            logger.warning("startup sweep: run for %s (pid %s) is no longer "
                           "alive; marking failed", sid, pid)
        self.journal.run_finished(sid, "failed")
        entry = self._state.get(sid)
        if entry is not None:
            entry["status"] = "failed"

    async def stop_work(self) -> None:
        tasks = list(self._active.values())
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)  # let pre-start-cancel emission tasks run

    # -- registry -------------------------------------------------------

    def upsert_session(self, local_id: str,
                       workspace: str | None = None) -> dict:
        """Create a session or change its workspace binding; journals the
        full entry. Memory is updated even if the append fails (the journal
        failure is logged); durability resumes with the next event."""
        cur = dict(self._state.get(local_id) or {})
        if workspace is not None:
            cur["ws"] = workspace
        cur.setdefault("omp", None)
        cur.setdefault("ws", None)
        cur.setdefault("status", "idle")
        self._state[local_id] = cur
        self.journal.session_upserted(local_id, cur["omp"], cur["ws"],
                                      cur["status"])
        return cur

    def refresh_titles(self) -> int:
        """Pull human session titles from omp's own store (read-only) and
        journal a session_titled event when one appears or changes, so the
        hub picker shows the local name. Returns events journaled. omp
        titles lazily and renames; absence is normal and never overwrites.
        Runs on the sync tick, before push_pending, so title events ride
        the same push."""
        if not self._state:
            return 0
        db_path = pathlib.Path(self.settings.omp_titles_db).expanduser()
        if not db_path.exists():
            return 0
        journaled = 0
        try:
            con = sqlite3.connect(
                f"file:{db_path}?mode=ro", uri=True, timeout=2)
            try:
                for sid, entry in self._state.items():
                    omp = entry.get("omp")
                    if not omp:
                        continue
                    row = con.execute(
                        "SELECT title FROM session_titles "
                        "WHERE session_id=?", (omp,)).fetchone()
                    title = str(row[0]).strip() if row and row[0] else ""
                    if title and title != entry.get("title"):
                        self.journal.titled(sid, title)
                        entry["title"] = title
                        journaled += 1
            finally:
                con.close()
        except sqlite3.Error:
            logger.warning("omp titles db unreadable (%s); skipping",
                           db_path, exc_info=True)
        return journaled

    def snapshot_rows(self) -> list[dict]:
        rows = []
        sids = list(self._state)
        sids += [s for s in self._active if s not in self._state]
        for sid in sids:
            rows.append({
                "local_id": sid,
                "status": "running" if sid in self._active else "idle",
                "workspace": (self._state.get(sid) or {}).get("ws"),
                # lets the hub render idle sessions as completed, not waiting
                "omp": (self._state.get(sid) or {}).get("omp"),
                "title": (self._state.get(sid) or {}).get("title"),
            })
        return rows

    def local_for_omp(self, omp: str | None) -> str | None:
        """Map an omp session id to its journal key. Discord-created
        sessions are journaled under the hub's id, but the hub's catalog
        reports the omp id once a run completes — without this mapping
        every completed Discord session looks unknown and gets re-adopted
        as an omp-id-keyed ghost."""
        if not omp:
            return None
        for sid, entry in self._state.items():
            if entry.get("omp") == omp:
                return sid
        return None

    # -- transcript journal ---------------------------------------------

    def journal_user_message(self, local_id: str, text: str,
                             request_id: str | None) -> None:
        self.journal.message(local_id, "user", text, _now(), request_id)
        self._messages.setdefault(local_id, []).append(
            {"role": "user", "content": text, "ts": _now(),
             "request_id": request_id})

    def journal_assistant_message(self, local_id: str, text: str,
                                  request_id: str | None) -> None:
        self.journal.message(local_id, "assistant", text, _now(), request_id)
        self._messages.setdefault(local_id, []).append(
            {"role": "assistant", "content": text, "ts": _now(),
             "request_id": request_id})

    def local_message_count(self, local_id: str) -> int:
        return len(self._messages.get(local_id, []))

    def hub_last_msg(self, local_id: str) -> int:
        return (self._state.get(local_id) or {}).get("hub_last_msg", 0)

    def merge_hub_messages(self, local_id: str, rows: list[dict]) -> int:
        """Mirror path: fold hub messages into the journal; skip ones this
        run already journaled (request_id match) — legacy rows without a
        request_id cannot collide (legacy journals carry no messages)."""
        known = {m.get("request_id")
                 for m in self._messages.get(local_id, [])
                 if m.get("request_id")}
        merged = 0
        for r in rows:
            rid = r.get("request_id")
            if rid and rid in known:
                continue
            role, content = r.get("role") or "assistant", r.get("content") or ""
            ts = str(r.get("created_at") or "")
            self.journal.message(local_id, role, content, ts, rid,
                                 mirrored=True)
            self._messages.setdefault(local_id, []).append(
                {"role": role, "content": content, "ts": ts,
                 "request_id": rid, "mirrored": True})
            if rid:
                known.add(rid)
            merged += 1
        self._state.setdefault(local_id, {})["hub_last_msg"] = max(
            [r.get("id", 0) for r in rows] + [self.hub_last_msg(local_id)])
        self.journal.hub_cursor(local_id, self.hub_last_msg(local_id))
        return merged

    # -- run lifecycle --------------------------------------------------

    def begin_run(self, local_id: str, text: str, request_id: str | None,
                  test_env: dict | None) -> str:
        """Accept one session.message. Returns 'started', 'busy' (a run is
        already active for this session), or 'duplicate' (request id already
        handled — acked, never re-spawned)."""
        if request_id is not None and request_id in self._seen_requests:
            logger.info("duplicate session.message request %s for %s; "
                        "already done", request_id, local_id)
            return "duplicate"
        if local_id in self._active:
            return "busy"
        if request_id is not None:
            self._seen_requests.append(request_id)
        self.journal_user_message(local_id, text, request_id)
        task = asyncio.create_task(
            self._guarded_run(local_id, text, request_id, test_env))
        task.add_done_callback(
            functools.partial(self._on_run_done, sid=local_id,
                              request_id=request_id))
        self._active[local_id] = task
        return "started"

    def cancel(self, local_id: str) -> str:
        task = self._active.get(local_id)
        if task is None:
            return "already_finished"
        task.cancel()
        return "accepted"

    def _runner_for(self, session_id: str) -> OmpRunner:
        if session_id not in self._runners:
            self._runners[session_id] = OmpRunner(
                self.settings.omp_bin,
                stream_limit=self.settings.stream_limit_bytes)
        return self._runners[session_id]

    def _stream_forwarder(self, sid: str, request_id: str | None):
        """Builds the omp on_event callback for one run. UX stream only:
        a failed delta/activity send must never fail the run itself."""
        async def on_event(ev: dict) -> None:
            try:
                await self._forward_stream_event(sid, request_id, ev)
            except Exception:
                logger.debug("stream forward failed for %s", sid, exc_info=True)
        return on_event

    async def _forward_stream_event(self, sid: str, request_id: str | None,
                                    ev: dict) -> None:
        t = ev.get("type")
        if t == "message_update":
            a = ev.get("assistantMessageEvent") or {}
            at = a.get("type", "")
            if at == "text_start":
                await self._emit(Envelope(
                    type=SESSION_DELTA, request_id=request_id, session_id=sid,
                    payload={"reset": True}))
            elif at == "text_delta":
                await self._emit(Envelope(
                    type=SESSION_DELTA, request_id=request_id, session_id=sid,
                    payload={"text": a.get("delta", "")}))
            elif at == "thinking_start":
                await self._emit(Envelope(
                    type=SESSION_ACTIVITY, request_id=request_id,
                    session_id=sid, payload={"kind": "think"}))
        elif t == "tool_execution_start":
            args = ev.get("args") or {}
            detail = (ev.get("intent")
                      or " ".join(str(v) for v in args.values() if v))[:120]
            await self._emit(Envelope(
                type=SESSION_ACTIVITY, request_id=request_id, session_id=sid,
                payload={"kind": "tool", "state": "start",
                         "tool": ev.get("toolName", "tool"),
                         "detail": detail}))
        elif t == "tool_execution_end":
            await self._emit(Envelope(
                type=SESSION_ACTIVITY, request_id=request_id, session_id=sid,
                payload={"kind": "tool", "state": "end",
                         "tool": ev.get("toolName", "tool")}))

    async def _run(self, sid: str, text: str, request_id: str | None,
                   test_env: dict | None) -> None:
        await self._emit(Envelope(type=SESSION_WAITING, request_id=request_id,
                                  session_id=sid, payload={"state": "running"}))
        runner = self._runner_for(sid)
        entry = self._state.get(sid) or {}
        workspace = entry.get("ws") or self.settings.default_workspace
        status = "failed"
        try:
            omp_sid, reply = await runner.run(
                workspace, entry.get("omp"), text, env=test_env,
                on_event=self._stream_forwarder(sid, request_id),
                on_spawn=lambda pid: self.journal.run_started(sid, pid))
            self.upsert_session(sid, workspace=workspace)
            self._state[sid]["omp"] = omp_sid
            self.journal.session_upserted(sid, omp_sid,
                                          self._state[sid].get("ws"), "idle")
            status = "idle"
            self.journal_assistant_message(sid, reply, request_id)
            await self._emit(Envelope(type=AGENT_MESSAGE, request_id=request_id,
                                      session_id=sid, payload={"text": reply}))
            await self._emit(Envelope(type=SESSION_COMPLETED,
                                      request_id=request_id, session_id=sid,
                                      payload={"omp_session_id": omp_sid}))
        except asyncio.CancelledError:
            try:
                await runner.cancel()
            except Exception:
                pass
            try:
                await self._emit(Envelope(type=SESSION_FAILED,
                                          request_id=request_id,
                                          session_id=sid,
                                          payload={"reason": "cancelled"}))
            except Exception:
                pass
        except Exception as exc:
            await self._emit(Envelope(type=SESSION_FAILED,
                                      request_id=request_id, session_id=sid,
                                      payload={"reason": str(exc)[:500]}))
        finally:
            self.journal.run_finished(sid, status)
            state_entry = self._state.get(sid)
            if state_entry is not None:
                state_entry["status"] = status
            self._active.pop(sid, None)

    def _on_run_done(self, task: asyncio.Task, sid: str,
                     request_id: str | None) -> None:
        # A task cancelled before its first step never executes its body, so
        # neither _run's handler nor _guarded_run's guard can run; emit here.
        if not task.cancelled():
            return
        self._active.pop(sid, None)

        async def _emit() -> None:
            try:
                await self._emit(Envelope(type=SESSION_FAILED,
                                          request_id=request_id,
                                          session_id=sid,
                                          payload={"reason": "cancelled"}))
            except Exception:
                pass

        asyncio.create_task(_emit())

    async def _guarded_run(self, sid: str, text: str, request_id: str | None,
                           test_env: dict | None) -> None:
        try:
            await self._run(sid, text, request_id, test_env)
        except asyncio.CancelledError:
            # Cancelled before _run's first step: nothing else emits or cleans up.
            self._active.pop(sid, None)
            try:
                await self._emit(Envelope(type=SESSION_FAILED,
                                          request_id=request_id,
                                          session_id=sid,
                                          payload={"reason": "cancelled"}))
            except Exception:
                pass

    async def _emit(self, env: Envelope) -> None:
        await self.on_event(env)
