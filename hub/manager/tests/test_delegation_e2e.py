"""E2E: two agents converse through a live hub; a third is denied.

The full delegation story in one flow: A discovers B via the directory
REST (agent token), opens a session on B through the agent-callable
POST /sessions, drives a two-round delegated conversation (needs_input
with options, then final), reads the transcript, grants C read — while C
is refused everywhere before the grant and stays read-only after; the
WSS-layer guards (hop cap, self-target) and admin oversight close it.

TestClient + three live sockets (test_ws pattern). Starlette 1.6's
WebSocketTestSession.receive_json has no deadline, so every socket gets a
pump thread (test_delegation._Socket); B is a fully scripted worker whose
replies come from a queue. AgentRows are pre-seeded via a direct
committed transaction after lifespan: StaticPool shares ONE sqlite
connection across sessions, and any REST session closing between
register()'s INSERT and its COMMIT silently erases the row (online
status still comes from ws registration — registry._conns)."""
import asyncio
import json
from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import AgentRow
from tests.test_delegation import (_Socket, _delegate, _mint, _register,
                                   _wait_online)

ADMIN = {"authorization": "Bearer adm"}

PROMPT_1 = "count the ducks at the pond"
PROMPT_2 = "and at the park?"
# B's scripted answers: (reply, kind, options) — one pop per round.
ANSWERS = [("42 ducks on the pond", "needs_input", ["pond", "park"]),
           ("final count: 42 ducks", "final", None)]


def _seed_agent(app, agent_id):
    """Insert the AgentRow before any registration. register() then takes
    the UPDATE path — its INSERT is the one StaticPool can lose (see
    test_routes._seed_agent for the full ruling)."""
    async def go():
        async with app.state.registry._maker() as s:
            s.add(AgentRow(id=agent_id, name=agent_id))
            await s.commit()
    asyncio.run(go())


def _greeter():
    """session.create → session.waiting echoing the RPC request_id: the
    hub's conn.call resolves on (session_id, request_id), not on type."""
    def waiting(env):
        return [{"protocol_version": 1, "type": "session.waiting",
                 "request_id": env.get("request_id"),
                 "session_id": env["session_id"],
                 "payload": {"state": "created"}}]
    return [("session.create", waiting)]


def _worker(answers):
    """B's scripted brain: greeter + session.message → the next queued
    answer, as agent.message (lands in the transcript) followed by
    session.completed carrying the round's kind/options (resolves A's
    pending delegation)."""
    def answer(env):
        reply, kind, options = answers.pop(0)
        out = [{"protocol_version": 1, "type": "agent.message",
                "request_id": env.get("request_id"),
                "session_id": env["session_id"],
                "payload": {"text": reply}}]
        done = {"protocol_version": 1, "type": "session.completed",
                "session_id": env["session_id"],
                "payload": {"omp_session_id": "omp-b", "kind": kind}}
        if options is not None:
            done["payload"]["options"] = options
        out.append(done)
        return out
    return _greeter() + [("session.message", answer)]


@contextmanager
def _trio(answers):
    """Live hub with agents A (requester), B (scripted worker) and C
    (outsider) online. Yields (client, ws_a, ws_b, ws_c, auth) with auth
    holding each agent's Bearer header."""
    app = create_app(Settings(admin_token="adm", sqlite_path=":memory:"))
    with TestClient(app) as client:  # lifespan: schema bootstrap first
        for aid in ("agent-a", "agent-b", "agent-c"):
            _seed_agent(app, aid)
        auth = {aid: {"authorization": f"Bearer {_mint(client, aid)}"}
                for aid in ("agent-a", "agent-b", "agent-c")}
        with client.websocket_connect(
                "/ws/agent", headers=auth["agent-b"]) as raw_b, \
             client.websocket_connect(
                "/ws/agent", headers=auth["agent-a"]) as raw_a, \
             client.websocket_connect(
                "/ws/agent", headers=auth["agent-c"]) as raw_c:
            ws_b = _Socket(raw_b, reactions=_worker(answers))
            ws_a = _Socket(raw_a, reactions=_greeter())
            ws_c = _Socket(raw_c)
            ws_b.send({"protocol_version": 1, "type": "agent.register",
                       "payload": {"agent_id": "agent-b", "name": "agent-b",
                                   "capabilities": ["chat", "cancel"],
                                   "description": "duck census bureau"}})
            ws_a.send(_register("agent-a", ["chat"]))
            ws_c.send(_register("agent-c", ["chat"]))
            _wait_online(client.app, "agent-a")
            _wait_online(client.app, "agent-b")
            _wait_online(client.app, "agent-c")
            yield client, ws_a, ws_b, ws_c, auth


def test_e2e_discovery_rounds_acl_and_guards():
    answers = [list(a) for a in ANSWERS]
    with _trio(answers) as (client, ws_a, ws_b, ws_c, auth):
        # 1. Discovery: A's agent token reads the directory; all three
        #    agents are online and B carries its registered metadata.
        r = client.get("/agents", headers=auth["agent-a"])
        assert r.status_code == 200, r.text
        directory = {a["id"]: a for a in r.json()}
        assert set(directory) == {"agent-a", "agent-b", "agent-c"}
        assert directory["agent-b"]["description"] == "duck census bureau"
        assert json.loads(directory["agent-b"]["capabilities"]) == \
            ["chat", "cancel"]
        assert all(a["online"] for a in directory.values())

        # 2. Nothing shared yet: A's listing is empty (it sees only
        #    hosted ∪ created ∪ granted sessions).
        r = client.get("/sessions", params={"agent": "agent-b"},
                       headers=auth["agent-a"])
        assert r.status_code == 200, r.text
        assert r.json() == []

        # 3. A opens a session on B with its agent token: A becomes the
        #    creator, the row is unowned, description is mandatory.
        r = client.post("/sessions", headers=auth["agent-a"],
                        json={"agent_id": "agent-b",
                              "description": "duck census"})
        assert r.status_code == 201, r.text
        sess = r.json()
        assert sess["creator_agent_id"] == "agent-a"
        assert sess["owner_id"] is None
        assert sess["agent_id"] == "agent-b"
        assert sess["description"] == "duck census"
        sid = sess["id"]
        assert ws_b.recv()["type"] == "session.create"

        # 4. Round 1: A delegates, B answers needs_input with options.
        _delegate(ws_a, sid, "req_r1", PROMPT_1)
        prompt = ws_b.recv()
        assert prompt["type"] == "session.message", prompt
        assert prompt["payload"]["text"] == PROMPT_1
        result = ws_a.recv()
        assert result["type"] == "delegation.result"
        assert result["request_id"] == "req_r1"
        assert result["session_id"] == sid
        assert result["payload"] == {"status": "completed",
                                     "reply": "42 ducks on the pond",
                                     "kind": "needs_input",
                                     "options": ["pond", "park"]}

        # 5. Round 2, same session: B answers final.
        _delegate(ws_a, sid, "req_r2", PROMPT_2)
        prompt = ws_b.recv()
        assert prompt["type"] == "session.message", prompt
        assert prompt["payload"]["text"] == PROMPT_2
        result = ws_a.recv()
        assert result["type"] == "delegation.result"
        assert result["request_id"] == "req_r2"
        assert result["payload"] == {"status": "completed",
                                     "reply": "final count: 42 ducks",
                                     "kind": "final"}

        # 6. The transcript holds both rounds: prompts and replies.
        r = client.get(f"/sessions/{sid}/messages", headers=auth["agent-a"])
        assert r.status_code == 200, r.text
        assert [(m["role"], m["content"]) for m in r.json()] == [
            ("user", PROMPT_1), ("assistant", "42 ducks on the pond"),
            ("user", PROMPT_2), ("assistant", "final count: 42 ducks")]

        # 7. C is a stranger: no read, no write.
        r = client.get(f"/sessions/{sid}", headers=auth["agent-c"])
        assert r.status_code == 403
        r = client.post(f"/sessions/{sid}/messages", headers=auth["agent-c"],
                        json={"text": "let me in"})
        assert r.status_code == 403

        # 8. The creator grants C read: transcript yes, prompts no.
        r = client.post(f"/sessions/{sid}/grants", headers=auth["agent-a"],
                        json={"agent_id": "agent-c", "level": "read"})
        assert r.status_code == 201, r.text
        r = client.get(f"/sessions/{sid}/messages", headers=auth["agent-c"])
        assert r.status_code == 200, r.text
        assert len(r.json()) == 4
        r = client.post(f"/sessions/{sid}/messages", headers=auth["agent-c"],
                        json={"text": "still no"})
        assert r.status_code == 403

        # 9a. Hop cap: hop=3 would be the 4th hop — refused as a loop
        #     before it reaches B.
        _delegate(ws_a, sid, "req_loop", "recurse deeper", hop=3)
        result = ws_a.recv()
        assert result["type"] == "delegation.result"
        assert result["request_id"] == "req_loop"
        assert result["session_id"] == sid
        assert result["payload"] == {"status": "failed", "error": "loop"}
        assert ws_b.frames.empty()

        # 9b. Self: a session hosted by agent-a itself (admin-created
        #     here; ws_a's greeter unblocks the open RPC).
        r = client.post("/sessions", headers=ADMIN,
                        json={"agent_id": "agent-a",
                              "owner_id": "discord:1"})
        assert r.status_code == 201, r.text
        sid_self = r.json()["id"]
        assert ws_a.recv()["type"] == "session.create"
        _delegate(ws_a, sid_self, "req_self", "talk to yourself")
        result = ws_a.recv()
        assert result["type"] == "delegation.result"
        assert result["request_id"] == "req_self"
        assert result["session_id"] == sid_self
        assert result["payload"] == {"status": "failed", "error": "self"}

        # 10. Oversight: admin sees every session.
        r = client.get("/sessions", headers=ADMIN)
        assert r.status_code == 200, r.text
        assert {s["id"] for s in r.json()} == {sid, sid_self}
