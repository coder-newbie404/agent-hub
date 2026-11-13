"""Delegation broker: correlates A→B prompt round-trips over the channel.

Mechanical only: ACL arithmetic, hop counting, occupancy, timeouts. The
hub never judges content — `kind`/`options` are B's own words, forwarded
verbatim."""
import asyncio
import logging
from dataclasses import dataclass

from app import acl
from app.protocol import DELEGATION_RESULT, Envelope, new_id
from app.sessions import AgentOffline, NotFound, RunInProgress

logger = logging.getLogger(__name__)


@dataclass
class Pending:
    requester: str
    timer: asyncio.Task | None = None


class DelegationBroker:
    def __init__(self, registry, sessions, maker,
                 result_timeout_s: float = 600.0, max_hop: int = 3,
                 send_timeout_s: float = 5.0):
        self.registry = registry
        self.sessions = sessions
        self.maker = maker
        self.result_timeout_s = result_timeout_s
        self.max_hop = max_hop
        self.send_timeout_s = send_timeout_s
        # (session_id, request_id) → Pending
        self._pending: dict[tuple[str, str], Pending] = {}
        self._replies: dict[str, str] = {}  # session_id → last agent.message

    async def on_delegate(self, conn, env: Envelope) -> None:
        sid = env.session_id or ""
        req = env.request_id or new_id("req")
        text = str(env.payload.get("text", ""))
        hop = int(env.payload.get("hop", 0))

        async def fail(error: str) -> None:
            await self._result(conn, sid, req, "failed", error=error)

        try:
            row = await self.sessions.get(sid)
        except NotFound:
            return await fail("not_found")
        if row.agent_id == conn.agent_id:
            return await fail("self")
        if not await acl.can_write(self.maker, conn.agent_id, row):
            return await fail("forbidden")
        if hop + 1 > self.max_hop:
            return await fail("loop")
        try:
            await self.sessions.user_message(sid, text, request_id=req)
        except RunInProgress:
            return await fail("busy")
        except AgentOffline:
            return await fail("agent_offline")
        except Exception:
            logger.exception("delegate.message → %s failed", sid)
            return await fail("error")
        timer = asyncio.create_task(self._timeout(conn, sid, req))
        self._pending[(sid, req)] = Pending(requester=conn.agent_id,
                                            timer=timer)

    async def _timeout(self, conn, sid: str, req: str) -> None:
        await asyncio.sleep(self.result_timeout_s)
        # Deliberately does NOT clear sessions.inflight: B's run still
        # owns the session. If B finishes later, on_session_finished
        # finds no pending entry and the late answer is dropped; if B
        # died, sessions.on_agent_offline resolves the run. Until then a
        # follow-up delegated prompt into the same session is refused
        # busy — A retries or opens another session (spec §10).
        if self._pending.pop((sid, req), None) is not None:
            await self._result(conn, sid, req, "failed", error="timeout")

    async def _result(self, conn, sid: str, req: str, status: str,
                      reply: str = "", error: str | None = None,
                      kind: str | None = None,
                      options: list | None = None) -> None:
        payload = {"status": status}
        if reply:
            payload["reply"] = reply  # spec: `reply?` — absent on failures
        if error:
            payload["error"] = error
        if kind:
            payload["kind"] = kind
        if options is not None:
            payload["options"] = options
        try:
            # bounded: a requester that cannot accept the result within the
            # window counts as gone — transcript keeps the answer either way
            # (spec §10). An unbounded send can park the hub loop when a
            # channel half-dies mid-teardown.
            await asyncio.wait_for(
                conn.send(Envelope(type=DELEGATION_RESULT, request_id=req,
                                   session_id=sid, payload=payload)),
                timeout=self.send_timeout_s)
        except Exception:
            logger.info("delegation result for %s lost (requester offline)",
                        req)

    def note_reply(self, session_id: str, text: str) -> None:
        self._replies[session_id] = text

    async def on_session_finished(self, session_id: str, outcome: str,
                                  payload: dict) -> None:
        key = None
        for (sid, req), pending in self._pending.items():
            if sid == session_id:
                key = (sid, req)
                break
        if key is None:
            return
        pending = self._pending.pop(key)
        if pending.timer:
            pending.timer.cancel()
        reply = self._replies.pop(session_id, "")
        conn = self.registry.get(pending.requester)
        if conn is None:
            return
        if outcome == "completed":
            await self._result(conn, session_id, key[1], "completed",
                               reply=reply,
                               kind=payload.get("kind"),
                               options=payload.get("options"))
        else:
            error = ("cancelled" if outcome == "cancelled"
                     else payload.get("reason", "failed"))
            await self._result(conn, session_id, key[1], "failed",
                               error=error)

    async def on_worker_offline(self, agent_id: str,
                                reason: str = "agent offline") -> None:
        """A worker channel died: every pending delegation into that
        agent's sessions resolves failed for the waiting requester. The
        runs themselves are sessions.on_agent_offline's business (the
        chain in main.py orders it first) — this only unblocks A. A
        requester that disconnected mid-round has no connection left, so
        nothing is sent and the transcript keeps B's answer (spec §10)."""
        sids = {sid for sid, _req in list(self._pending)}
        for sid in sids:
            try:
                row = await self.sessions.get(sid)
            except NotFound:
                continue
            if row.agent_id != agent_id:
                continue
            for (s2, req) in [k for k in list(self._pending) if k[0] == sid]:
                pending = self._pending.pop((s2, req))
                if pending.timer:
                    pending.timer.cancel()
                conn = self.registry.get(pending.requester)
                if conn:
                    await self._result(conn, s2, req, "failed",
                                       error="agent_offline")


def chain_offline(*handlers):
    """Fan one agent-offline event out to several handlers; main.py wires
    sessions.on_agent_offline + DelegationBroker.on_worker_offline here —
    the single registry.on_offline wiring point."""
    async def _all(agent_id: str, reason: str = "agent offline"):
        for h in handlers:
            try:
                await h(agent_id, reason)
            except Exception:
                logger.exception("offline handler %s failed", h)
    return _all
