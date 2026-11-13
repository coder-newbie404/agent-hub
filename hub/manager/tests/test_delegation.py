"""DelegationBroker happy path: an agent-channel delegate.message round
relayed back to the requester as delegation.result.

TestClient two-live-sockets pattern (test_ws.py). Starlette 1.6's
WebSocketTestSession.receive_json has no deadline, and the open_session RPC
(conn.call) would deadlock the single test thread — so each socket gets a
pump thread: frames land in a queue (recv takes a timeout) and the worker
socket carries one scripted reaction (session.create → session.waiting echo)
that unblocks the hub's create RPC."""
import queue
import threading
import time
from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.config import Settings
from app.delegation import chain_offline
from app.main import create_app

ADMIN = {"authorization": "Bearer adm"}


def _mint(client, agent_id):
    r = client.post("/tokens", headers=ADMIN, json={"agent_id": agent_id})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _register(agent_id, capabilities):
    return {"protocol_version": 1, "type": "agent.register",
            "payload": {"agent_id": agent_id, "name": agent_id,
                        "capabilities": capabilities}}


def _wait_online(app, agent_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app.state.registry.online(agent_id):
            return
        time.sleep(0.01)
    raise AssertionError(f"{agent_id} never came online")


def _wait_offline(app, agent_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not app.state.registry.online(agent_id):
            return
        time.sleep(0.01)
    raise AssertionError(f"{agent_id} never went offline")


class _Socket:
    """Background pump for one live socket."""

    def __init__(self, ws, reactions=()):
        self.frames = queue.Queue()
        self._ws = ws
        self._reactions = list(reactions)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            while True:
                env = self._ws.receive_json()
                self.frames.put(env)
                for match, reply in self._reactions:
                    if env["type"] == match:
                        for out in reply(env):
                            self._ws.send_json(out)
        except Exception:
            self.frames.put(None)  # socket closed → failing sentinel

    def recv(self, timeout=5.0):
        env = self.frames.get(timeout=timeout)
        if env is None:
            raise AssertionError("socket closed before a frame arrived")
        return env

    def send(self, env):
        self._ws.send_json(env)

    def close(self):
        self._ws.close()


def _worker():
    """session.create → session.waiting echoing the RPC request_id: the
    hub's conn.call resolves on (session_id, request_id), not on type."""
    def waiting(env):
        return [{"protocol_version": 1, "type": "session.waiting",
                 "request_id": env.get("request_id"),
                 "session_id": env["session_id"],
                 "payload": {"state": "created"}}]
    return [("session.create", waiting)]


@contextmanager
def _pair(client):
    """agent-b online (worker reactions scripted) + agent-a online (the
    requester). Yields (requester socket, worker socket)."""
    sec_b = _mint(client, "agent-b")
    sec_a = _mint(client, "agent-a")
    with client.websocket_connect(
            "/ws/agent",
            headers={"authorization": f"Bearer {sec_b}"}) as raw_b, \
         client.websocket_connect(
            "/ws/agent",
            headers={"authorization": f"Bearer {sec_a}"}) as raw_a:
        ws_b = _Socket(raw_b, reactions=_worker())
        ws_a = _Socket(raw_a)
        app = client.app
        ws_b.send(_register("agent-b", ["chat", "cancel"]))
        ws_a.send(_register("agent-a", ["chat"]))
        _wait_online(app, "agent-b")
        _wait_online(app, "agent-a")
        try:
            yield ws_a, ws_b
        finally:
            # Graceful close before the raw sessions unwind: starlette's
            # session exit sends websocket.disconnect and then *immediately*
            # cancels the app task, so without this the cancellation can land
            # mid-DB-statement (frame handler or the offline unwind) and
            # hard-invalidate the pooled connection. Sending the disconnect
            # ourselves and waiting for the server-side offline marking means
            # the app task is parked at its trailing sleep when the portal
            # cancels it. A second disconnect frame is harmless: the receive
            # loop is already gone and the socket buffers it unread.
            ws_b.close()
            ws_a.close()
            _wait_offline(app, "agent-b")
            _wait_offline(app, "agent-a")


def _open_session(client, grant=True):
    """Session on B, admin-created (creator_agent_id None) — A is not the
    creator, so without a grant its delegation is refused. grant=False
    leaves the ACL untouched (the forbidden test flips it on later)."""
    r = client.post("/sessions", headers=ADMIN,
                    json={"agent_id": "agent-b", "owner_id": "discord:1",
                          "workspace": None})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    if grant:
        g = client.post(f"/sessions/{sid}/grants", headers=ADMIN,
                        json={"agent_id": "agent-a", "level": "write"})
        assert g.status_code == 201, g.text
    return sid


@contextmanager
def _scaffold(app, grant_write=True):
    """Happy-path preamble shared by every delegation test: live A+B
    sockets (B with the scripted worker reactions), a session on B, and —
    by default — A's write grant. B's session.create frame is consumed.
    Yields (client, ws_a, ws_b, sid)."""
    with TestClient(app) as client, _pair(client) as (ws_a, ws_b):
        sid = _open_session(client, grant=grant_write)
        assert ws_b.recv()["type"] == "session.create"
        yield client, ws_a, ws_b, sid


def _delegate(ws_a, sid, request_id, text, hop=0):
    ws_a.send({"protocol_version": 1, "type": "delegate.message",
               "request_id": request_id, "session_id": sid,
               "payload": {"text": text, "hop": hop}})


def _answer(ws_b, sid, text, reply):
    env = ws_b.recv()
    assert env["type"] == "session.message", env
    assert env["payload"]["text"] == text
    ws_b.send({"protocol_version": 1, "type": "agent.message",
               "request_id": env["request_id"], "session_id": sid,
               "payload": {"text": reply}})
    return env


def test_delegation_happy_path_relays_result():
    app = create_app(Settings(admin_token="adm", sqlite_path=":memory:"))
    with _scaffold(app) as (client, ws_a, ws_b, sid):
        _delegate(ws_a, sid, "req_d1", "list the ducks")
        _answer(ws_b, sid, "list the ducks", "there are 3 ducks")
        ws_b.send({"protocol_version": 1, "type": "session.completed",
                   "session_id": sid,
                   "payload": {"omp_session_id": "omp-1",
                               "kind": "final"}})
        result = ws_a.recv()
        assert result["type"] == "delegation.result"
        assert result["request_id"] == "req_d1"
        assert result["session_id"] == sid
        assert result["payload"]["status"] == "completed"
        assert result["payload"]["reply"] == "there are 3 ducks"
        assert result["payload"]["kind"] == "final"


def test_delegation_second_round_same_session():
    app = create_app(Settings(admin_token="adm", sqlite_path=":memory:"))
    with _scaffold(app) as (client, ws_a, ws_b, sid):
        _delegate(ws_a, sid, "req_d1", "list the ducks")
        _answer(ws_b, sid, "list the ducks", "there are 3 ducks")
        ws_b.send({"protocol_version": 1, "type": "session.completed",
                   "session_id": sid,
                   "payload": {"omp_session_id": "omp-1",
                               "kind": "final"}})
        first = ws_a.recv()
        assert first["payload"]["status"] == "completed"
        # a completed session accepts another delegated prompt: the run is
        # no longer in flight, so B receives session.message again
        _delegate(ws_a, sid, "req_d2", "how many geese?")
        _answer(ws_b, sid, "how many geese?", "3 geese")
        ws_b.send({"protocol_version": 1, "type": "session.completed",
                   "session_id": sid,
                   "payload": {"omp_session_id": "omp-1",
                               "kind": "final"}})
        second = ws_a.recv()
        assert second["type"] == "delegation.result"
        assert second["request_id"] == "req_d2"
        assert second["session_id"] == sid
        assert second["payload"]["status"] == "completed"
        assert second["payload"]["reply"] == "3 geese"


def test_delegation_result_passes_kind_and_options_through():
    app = create_app(Settings(admin_token="adm", sqlite_path=":memory:"))
    with _scaffold(app) as (client, ws_a, ws_b, sid):
        _delegate(ws_a, sid, "req_d3", "list the ducks")
        _answer(ws_b, sid, "list the ducks", "there are 3 ducks")
        ws_b.send({"protocol_version": 1, "type": "session.completed",
                   "session_id": sid,
                   "payload": {"omp_session_id": "omp-1",
                               "kind": "needs_input",
                               "options": ["red", "blue"]}})
        result = ws_a.recv()
        assert result["type"] == "delegation.result"
        assert result["request_id"] == "req_d3"
        assert result["payload"]["status"] == "completed"
        assert result["payload"]["kind"] == "needs_input"
        assert result["payload"]["options"] == ["red", "blue"]


def test_timeout_fails_pending_delegation():
    app = create_app(Settings(admin_token="adm", sqlite_path=":memory:",
                              delegation_result_timeout_s=0.2))
    with _scaffold(app) as (client, ws_a, ws_b, sid):
        _delegate(ws_a, sid, "req_t1", "list the ducks")
        assert ws_b.recv()["type"] == "session.message"
        # B does not answer: the broker's deadline resolves the round.
        result = ws_a.recv(timeout=5.0)
        assert result["type"] == "delegation.result"
        assert result["request_id"] == "req_t1"
        assert result["session_id"] == sid
        assert result["payload"] == {"status": "failed", "error": "timeout"}
        # The timeout pops only the broker's pending entry: B's run still
        # owns the session (inflight stays set) — a follow-up delegated
        # prompt is refused busy until B's run resolves (completed → the
        # late answer is dropped; died → sessions.on_agent_offline fails
        # the run).
        _delegate(ws_a, sid, "req_t2", "still there?")
        followup = ws_a.recv(timeout=5.0)
        assert followup["request_id"] == "req_t2"
        assert followup["payload"] == {"status": "failed", "error": "busy"}


def test_self_delegation_rejected():
    app = create_app(Settings(admin_token="adm", sqlite_path=":memory:"))
    with _scaffold(app) as (client, ws_a, ws_b, sid):
        # B delegates into its own hosted session.
        _delegate(ws_b, sid, "req_self", "talk to yourself")
        result = ws_b.recv(timeout=5.0)
        assert result["type"] == "delegation.result"
        assert result["request_id"] == "req_self"
        assert result["session_id"] == sid
        assert result["payload"] == {"status": "failed", "error": "self"}


def test_hop_cap_rejected():
    app = create_app(Settings(admin_token="adm", sqlite_path=":memory:"))
    with _scaffold(app) as (client, ws_a, ws_b, sid):
        # max_hop defaults to 3: a prompt that already made 3 hops would
        # be the 4th — refused as a loop before it reaches B.
        _delegate(ws_a, sid, "req_hop", "deeper down the rabbit hole",
                  hop=3)
        result = ws_a.recv(timeout=5.0)
        assert result["type"] == "delegation.result"
        assert result["request_id"] == "req_hop"
        assert result["payload"] == {"status": "failed", "error": "loop"}


def test_worker_offline_fails_pending():
    app = create_app(Settings(admin_token="adm", sqlite_path=":memory:"))
    with _scaffold(app) as (client, ws_a, ws_b, sid):
        _delegate(ws_a, sid, "req_off", "list the ducks")
        assert ws_b.recv()["type"] == "session.message"
        # B's channel dies mid-run. agent-b registered WITHOUT
        # owns_sessions → the legacy path fails the in-flight session via
        # sessions.on_agent_offline; the chained delegation hook resolves
        # A's pending round on the same event.
        ws_b.close()
        result = ws_a.recv(timeout=5.0)
        assert result["type"] == "delegation.result"
        assert result["request_id"] == "req_off"
        assert result["session_id"] == sid
        assert result["payload"] == {"status": "failed",
                                     "error": "agent_offline"}


def test_busy_session_returns_busy():
    app = create_app(Settings(admin_token="adm", sqlite_path=":memory:"))
    with _scaffold(app) as (client, ws_a, ws_b, sid):
        # the first prompt occupies the session: B never completes it
        _delegate(ws_a, sid, "req_b1", "first prompt")
        # a second delegated prompt into the same session is refused
        _delegate(ws_a, sid, "req_b2", "second prompt")
        result = ws_a.recv(timeout=5.0)
        assert result["type"] == "delegation.result"
        assert result["request_id"] == "req_b2"
        assert result["payload"] == {"status": "failed", "error": "busy"}
        # ...and the refusal left round one intact: B still got the first
        # prompt and its answer still reaches A (drains req_b1's timer).
        _answer(ws_b, sid, "first prompt", "first answer")
        ws_b.send({"protocol_version": 1, "type": "session.completed",
                   "session_id": sid,
                   "payload": {"omp_session_id": "omp-1",
                               "kind": "final"}})
        first = ws_a.recv(timeout=5.0)
        assert first["request_id"] == "req_b1"
        assert first["payload"]["status"] == "completed"
        assert first["payload"]["reply"] == "first answer"


def test_forbidden_without_write():
    app = create_app(Settings(admin_token="adm", sqlite_path=":memory:"))
    # session created by admin (creator_agent_id None); A has no grant.
    with _scaffold(app, grant_write=False) as (client, ws_a, ws_b, sid):
        _delegate(ws_a, sid, "req_forb", "list the ducks")
        result = ws_a.recv(timeout=5.0)
        assert result["type"] == "delegation.result"
        assert result["request_id"] == "req_forb"
        assert result["payload"] == {"status": "failed",
                                     "error": "forbidden"}
        assert ws_b.frames.empty()  # nothing reached B
        # admin grants write; the retry goes through end-to-end
        g = client.post(f"/sessions/{sid}/grants", headers=ADMIN,
                        json={"agent_id": "agent-a", "level": "write"})
        assert g.status_code == 201, g.text
        _delegate(ws_a, sid, "req_ok", "list the ducks")
        # prompt reaches B, and the full round now completes for A
        _answer(ws_b, sid, "list the ducks", "3 ducks")
        ws_b.send({"protocol_version": 1, "type": "session.completed",
                   "session_id": sid,
                   "payload": {"omp_session_id": "omp-1",
                               "kind": "final"}})
        ok = ws_a.recv(timeout=5.0)
        assert ok["request_id"] == "req_ok"
        assert ok["payload"]["status"] == "completed"


async def test_chain_offline_isolates_raising_handler():
    # one handler raising must not skip the rest: the sweep backstop and
    # the ws disconnect unwinds fan out through chain_offline, so a single
    # broken handler is logged and swallowed, never blocking its peers.
    ran = []

    async def boom(agent_id, reason):
        raise RuntimeError("handler exploded")

    async def record(agent_id, reason):
        ran.append((agent_id, reason))

    await chain_offline(boom, record)("agent-b", "agent offline")
    assert ran == [("agent-b", "agent offline")]
