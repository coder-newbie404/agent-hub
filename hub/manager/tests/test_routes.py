import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from types import SimpleNamespace

from app.config import Settings
from app.main import create_app
from app.models import AgentRow, MessageRow, SessionRow
from app.sessions import (AgentOffline, NotFound, RunInProgress,
                          SessionError, upsert_projected)
from tests.test_delegation import (_Socket, _mint, _register, _wait_online,
                                   _worker)


def client() -> TestClient:
    return TestClient(create_app(Settings(admin_token="t", sqlite_path=":memory:")))


def test_health_open():
    with client() as c:  # lifespan runs schema bootstrap for the in-memory DB
        assert c.get("/health").json() == {"status": "ok"}


def test_interactive_docs_and_schema_disabled():
    # unauthenticated /docs, /redoc and /openapi.json would publish the whole
    # API surface through the reverse proxy
    with client() as c:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert c.get(path).status_code == 404, path
        assert c.get("/health").status_code == 200


def test_agents_requires_token():
    with client() as c:
        assert c.get("/agents").status_code == 401
        r = c.get("/agents", headers={"authorization": "Bearer t"})
        assert r.status_code == 200 and r.json() == []


def test_get_unknown_session_404():
    with client() as c:
        r = c.get("/sessions/sess_nope", headers={"authorization": "Bearer t"})
        assert r.status_code == 404


def test_non_ascii_token_rejected_not_500():
    # scanners send raw non-ASCII Authorization bytes; the comparison must
    # answer 401 (constant-time, bytes) instead of raising TypeError
    class FakeState:
        settings = Settings(admin_token="t")

    class FakeApp:
        state = FakeState()

    class FakeRequest:
        app = FakeApp()
        headers = {"authorization": "Bearer tØken"}  # non-ASCII, as latin-1-decoded

    from app.routes import require_admin
    with pytest.raises(HTTPException) as exc_info:
        require_admin(FakeRequest())
    assert exc_info.value.status_code == 401


def _client() -> TestClient:
    return TestClient(create_app(Settings(admin_token="adm", sqlite_path=":memory:")))


def test_token_endpoints_require_admin():
    with _client() as c:
        assert c.post("/tokens", json={"agent_id": "omp-local"}).status_code == 401
        assert c.get("/tokens").status_code == 401


def test_mint_rotate_revoke_flow():
    auth = {"authorization": "Bearer adm"}
    with _client() as c:  # lifespan seeds the bootstrap token
        r = c.post("/tokens", json={"agent_id": "omp-local", "ttl_days": 7},
                   headers=auth)
        assert r.status_code == 200
        body = r.json()
        assert body["token"].startswith("ask_") and body["agent_id"] == "omp-local"
        assert body["expires_at"] and body["expires_at"] != "None"
        token_id = body["token_id"]

        listed = c.get("/tokens", headers=auth).json()
        assert token_id in [t["token_id"] for t in listed]
        assert all("token" not in t for t in listed)  # plaintext never re-served

        r = c.post("/tokens/omp-local/rotate", headers=auth)
        assert r.status_code == 200
        new_token = r.json()["token"]
        assert new_token != body["token"]

        r = c.post("/tokens/omp-local/revoke", headers=auth)
        assert r.json()["revoked"] >= 1

        # the minted secrets can no longer authenticate REST as bootstrap
        assert c.get("/agents", headers={
            "authorization": f"Bearer {new_token}"}).status_code == 401
        # bootstrap admin still works
        assert c.get("/agents", headers={
            "authorization": "Bearer adm"}).status_code == 200


def test_bootstrap_token_cannot_access_rest():
    """ADMIN_TOKEN is also the wildcard WS bootstrap secret; REST stays
    admin-gated — same secret, different endpoint class."""
    with _client() as c:
        assert c.get("/agents", headers={
            "authorization": "Bearer adm"}).status_code == 200


# --- frontend (Discord bot) REST surface ---------------------------------
# The extracted frontend component owns no DB: everything it used to reach
# in-process crosses these endpoints. Tests pin the HTTP contract.


@pytest.fixture
async def hub():
    """App with lifespan run (schema bootstrap) under an ASGI transport."""
    app = create_app(Settings(admin_token="t", sqlite_path=":memory:"))
    async with app.router.lifespan_context(app):
        yield app


async def call(app, method, path, auth=True, **kw):
    headers = kw.pop("headers", None)
    if headers is None and auth:
        headers = {"authorization": "Bearer t"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        return await c.request(method, path, headers=headers, **kw)


async def _seed_session(app, omp="omp-abc", status="waiting", thread_id=None):
    row = await upsert_projected(app.state.sessions._maker, "omp-local", omp,
                                 status=status)
    if thread_id is not None:
        async with app.state.sessions._maker() as s:
            await s.execute(update(SessionRow)
                            .where(SessionRow.id == row.id)
                            .values(thread_id=thread_id))
            await s.commit()
    return row


def _row_stub(**kw):
    base = dict(id="sess_x", agent_id="a1", owner_id="o", thread_id=None,
                status="waiting", workspace="/w", title=None, archived=False,
                omp_session_id=None, created_at=datetime(2026, 10, 2),
                updated_at=datetime(2026, 10, 2),
                creator_agent_id=None, description="", last_used_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


async def test_sessions_carry_timestamps(hub):
    row = await _seed_session(hub)
    r = await call(hub, "GET", "/sessions")
    assert r.status_code == 200
    body = r.json()[0]
    assert body["created_at"] and "T" in body["created_at"]
    assert body["updated_at"] and "T" in body["updated_at"]
    r = await call(hub, "GET", f"/sessions/{row.id}")
    assert r.json()["created_at"] == body["created_at"]

async def test_find_session_by_thread(hub):
    row = await _seed_session(hub, thread_id=555)
    r = await call(hub, "GET", "/sessions", params={"thread_id": 555})
    assert r.status_code == 200 and r.json()["id"] == row.id
    r = await call(hub, "GET", "/sessions", params={"thread_id": 1})
    assert r.status_code == 404


async def test_message_listing_and_unposted_filter(hub):
    row = await _seed_session(hub)
    async with hub.state.sessions._maker() as s:
        s.add(MessageRow(session_id=row.id, role="user", content="q",
                         posted_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        s.add(MessageRow(session_id=row.id, role="assistant", content="a"))
        await s.commit()
    r = await call(hub, "GET", f"/sessions/{row.id}/messages")
    body = r.json()
    assert [m["content"] for m in body] == ["q", "a"]
    assert body[0]["posted_at"] and body[1]["posted_at"] is None
    assert body[1]["id"] > 0
    r = await call(hub, "GET", f"/sessions/{row.id}/messages",
                   params={"unposted": "true"})
    assert [m["content"] for m in r.json()] == ["a"]


async def test_mark_posted(hub):
    row = await _seed_session(hub)
    async with hub.state.sessions._maker() as s:
        m = MessageRow(session_id=row.id, role="assistant", content="x")
        s.add(m)
        await s.commit()
        mid = m.id
    r = await call(hub, "POST", f"/sessions/{row.id}/messages/mark-posted",
                   json={"ids": [mid]})
    assert r.json() == {"marked": 1}
    async with hub.state.sessions._maker() as s:
        got = (await s.execute(select(MessageRow)
                               .where(MessageRow.id == mid))).scalars().one()
    assert got.posted_at is not None


async def test_missed_session_ids(hub):
    a = await _seed_session(hub, omp="omp-a")
    await _seed_session(hub, omp="omp-b")
    async with hub.state.sessions._maker() as s:
        s.add(MessageRow(session_id=a.id, role="assistant", content="m"))
        await s.commit()
    r = await call(hub, "GET", "/sessions/missed")
    assert r.json() == {"session_ids": [a.id]}


async def test_recover_interrupted_over_rest(hub):
    await _seed_session(hub, omp="omp-r", status="running")
    r = await call(hub, "POST", "/sessions/recover-interrupted")
    ids = r.json()["session_ids"]
    assert len(ids) == 1
    row = await hub.state.sessions.get(ids[0])
    assert row.status == "failed"


async def test_run_status_reports_inflight_and_activity(hub):
    row = await _seed_session(hub)
    hub.state.sessions.inflight[row.id] = "req_x"
    hub.state.sessions.activity[row.id] = "bash: ls"
    r = await call(hub, "GET", f"/sessions/{row.id}/run")
    assert r.json() == {"inflight": True, "activity": "bash: ls"}
    hub.state.sessions.inflight.pop(row.id)
    hub.state.sessions.activity.pop(row.id)
    r = await call(hub, "GET", f"/sessions/{row.id}/run")
    assert r.json() == {"inflight": False, "activity": None}


async def test_agents_rows_carry_live_online(hub, monkeypatch):
    async def rows():
        return [SimpleNamespace(id="a1", name="A", status="offline",
                                capabilities="[]", description="",
                                tools="[]", environment="{}", last_seen=None)]
    monkeypatch.setattr(hub.state.registry, "list", rows)
    body = (await call(hub, "GET", "/agents")).json()
    assert body[0]["online"] is False
    hub.state.registry._conns["a1"] = SimpleNamespace()
    body = (await call(hub, "GET", "/agents")).json()
    assert body[0]["online"] is True


async def test_open_session_over_rest(hub, monkeypatch):
    async def ok(agent_id, owner_id, workspace, title=None):
        assert (agent_id, owner_id, workspace) == ("a1", "o1", "/w")
        return _row_stub(agent_id=agent_id)
    monkeypatch.setattr(hub.state.sessions, "open_session", ok)
    r = await call(hub, "POST", "/sessions",
                   json={"agent_id": "a1", "owner_id": "o1", "workspace": "/w"})
    assert r.status_code == 201 and r.json()["id"] == "sess_x"

    async def offline(*a, **k):
        raise AgentOffline("agent a1 is offline")
    monkeypatch.setattr(hub.state.sessions, "open_session", offline)
    r = await call(hub, "POST", "/sessions",
                   json={"agent_id": "a1", "owner_id": "o"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "agent_offline"

    async def failed(*a, **k):
        raise SessionError("create failed")
    monkeypatch.setattr(hub.state.sessions, "open_session", failed)
    r = await call(hub, "POST", "/sessions",
                   json={"agent_id": "a1", "owner_id": "o"})
    assert r.status_code == 502
    assert r.json()["detail"]["code"] == "agent_failed"


async def test_send_message_over_rest(hub, monkeypatch):
    async def ok(session_id, text):
        assert (session_id, text) == ("sess_x", "hi")
        return "req_1"
    monkeypatch.setattr(hub.state.sessions, "user_message", ok)
    r = await call(hub, "POST", "/sessions/sess_x/messages", json={"text": "hi"})
    assert r.json() == {"request_id": "req_1"}

    async def busy(*a, **k):
        raise RunInProgress("busy")
    monkeypatch.setattr(hub.state.sessions, "user_message", busy)
    r = await call(hub, "POST", "/sessions/sess_x/messages", json={"text": "hi"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "run_in_progress"

    async def off(*a, **k):
        raise AgentOffline("offline")
    monkeypatch.setattr(hub.state.sessions, "user_message", off)
    r = await call(hub, "POST", "/sessions/sess_x/messages", json={"text": "hi"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "agent_offline"

    async def nf(*a, **k):
        raise NotFound("nope")
    monkeypatch.setattr(hub.state.sessions, "user_message", nf)
    assert (await call(hub, "POST", "/sessions/sess_x/messages",
                       json={"text": "hi"})).status_code == 404


async def test_cancel_over_rest(hub, monkeypatch):
    async def ok(session_id):
        return True
    monkeypatch.setattr(hub.state.sessions, "cancel", ok)
    r = await call(hub, "POST", "/sessions/sess_x/cancel")
    assert r.json() == {"cancelled": True}


async def test_patch_updates_fields(hub):
    row = await _seed_session(hub)
    r = await call(hub, "PATCH", f"/sessions/{row.id}", json={"title": "T"})
    assert r.status_code == 200 and r.json()["title"] == "T"
    r = await call(hub, "PATCH", f"/sessions/{row.id}", json={"archived": True})
    assert r.json()["archived"] is True
    r = await call(hub, "PATCH", f"/sessions/{row.id}", json={"thread_id": 42})
    assert r.json()["thread_id"] == 42
    r = await call(hub, "PATCH", "/sessions/sess_nope", json={"title": "x"})
    assert r.status_code == 404


async def test_delete_over_rest(hub, monkeypatch):
    calls = []

    async def ok(session_id):
        calls.append(session_id)
    monkeypatch.setattr(hub.state.sessions, "delete", ok)
    r = await call(hub, "DELETE", "/sessions/sess_x")
    assert r.status_code == 204 and calls == ["sess_x"]

    async def busy(session_id):
        raise RunInProgress("run")
    monkeypatch.setattr(hub.state.sessions, "delete", busy)
    assert (await call(hub, "DELETE", "/sessions/sess_x")).status_code == 409

    async def nf(session_id):
        raise NotFound("no")
    monkeypatch.setattr(hub.state.sessions, "delete", nf)
    assert (await call(hub, "DELETE", "/sessions/sess_x")).status_code == 404


class _Conn:
    def __init__(self):
        self.sent = []

    async def send(self, env):
        self.sent.append(env)


async def test_sync_nudge(hub):
    reg = hub.state.registry
    reg.set_projection("a1", True)
    conn = _Conn()
    reg._conns["a1"] = conn
    r = await call(hub, "POST", "/sync/nudge")
    assert r.json() == {"nudged": 1}
    assert sent_types(conn) == ["sync.now"]


def sent_types(conn):
    return [e.type for e in conn.sent]


async def test_message_count(hub):
    row = await _seed_session(hub)
    async with hub.state.sessions._maker() as s:
        s.add(MessageRow(session_id=row.id, role="user", content="1"))
        s.add(MessageRow(session_id=row.id, role="assistant", content="2"))
        await s.commit()
    r = await call(hub, "GET", f"/sessions/{row.id}/messages/count")
    assert r.json() == {"count": 2}


def test_agents_view_includes_metadata():
    with client() as c:
        r = c.get("/agents", headers={"authorization": "Bearer t"})
        assert r.status_code == 200
        # empty registry → empty list; metadata assertions land in Task 4
        # once rows are seeded via agent-token registration
        assert r.json() == []


# --- require_agent: principal resolution for agent-callable routes --------


def _fake_request(app, auth):
    # same shape as the FakeRequest in test_non_ascii_token_rejected_not_500
    return SimpleNamespace(app=app, headers={"authorization": auth})


@pytest.mark.asyncio
async def test_require_agent_admin_and_pinned(hub):
    from app.routes import require_agent
    # admin bootstrap token → None (admin principal)
    assert await require_agent(_fake_request(hub, "Bearer t")) is None


@pytest.mark.asyncio
async def test_require_agent_resolves_pinned_token(hub):
    from app.routes import require_agent
    _, secret = await hub.state.tokens.issue("agent-x")
    assert await require_agent(
        _fake_request(hub, f"Bearer {secret}")) == "agent-x"


@pytest.mark.asyncio
async def test_require_agent_rejects_garbage(hub):
    from app.routes import require_agent
    with pytest.raises(HTTPException) as ei:
        await require_agent(_fake_request(hub, "Bearer nope"))
    assert ei.value.status_code == 401


# --- agent principals: scoped directory + sessions + grants ---------------


@pytest.fixture
async def adm_hub():
    """Like hub, but ADMIN_TOKEN 'adm': ACL tests distinguish the admin
    bootstrap token from per-agent tokens explicitly."""
    app = create_app(Settings(admin_token="adm", sqlite_path=":memory:"))
    async with app.router.lifespan_context(app):
        yield app


async def _acl_seed(app, **kw):
    row = SessionRow(id=kw["id"], agent_id=kw["agent_id"], owner_id=None,
                     creator_agent_id=kw.get("creator"),
                     description=kw.get("description", ""), status="waiting")
    async with app.state.sessions._maker() as s:
        s.add(row)
        await s.commit()
    return row


async def test_agent_scoped_listing_and_detail(adm_hub):
    app = adm_hub
    _, tok_b = await app.state.tokens.issue("agent-b")
    await _acl_seed(app, id="sess_mine", agent_id="agent-b",
                    creator="agent-b", description="d")
    await _acl_seed(app, id="sess_other", agent_id="agent-c",
                    creator="agent-c")
    r = await call(app, "GET", "/sessions",
                   headers={"authorization": f"Bearer {tok_b}"})
    assert [s["id"] for s in r.json()] == ["sess_mine"]
    r = await call(app, "GET", "/sessions/sess_other",
                   headers={"authorization": f"Bearer {tok_b}"})
    assert r.status_code == 403
    r = await call(app, "GET", "/sessions/sess_mine",
                   headers={"authorization": f"Bearer {tok_b}"})
    assert r.status_code == 200
    assert r.json()["creator_agent_id"] == "agent-b"
    assert r.json()["description"] == "d"
    assert r.json()["last_used_at"] is None
    assert r.json()["lifecycle"] == "active"
    # admin still sees everything
    r = await call(app, "GET", "/sessions",
                   headers={"authorization": "Bearer adm"})
    assert {s["id"] for s in r.json()} == {"sess_mine", "sess_other"}


async def test_grants_endpoints(adm_hub):
    app = adm_hub
    _, tok_b = await app.state.tokens.issue("agent-b")
    _, tok_c = await app.state.tokens.issue("agent-c")
    await _acl_seed(app, id="sess_mine", agent_id="agent-b",
                    creator="agent-b", description="d")
    auth_b = {"authorization": f"Bearer {tok_b}"}
    auth_c = {"authorization": f"Bearer {tok_c}"}
    # creator grants agent-c read on sess_mine → visible
    r = await call(app, "POST", "/sessions/sess_mine/grants", headers=auth_b,
                   json={"agent_id": "agent-c", "level": "read"})
    assert r.status_code == 201
    r = await call(app, "GET", "/sessions", headers=auth_c)
    assert [s["id"] for s in r.json()] == ["sess_mine"]
    r = await call(app, "GET", "/sessions/sess_mine", headers=auth_c)
    assert r.status_code == 200
    # revoke → gone from the listing and the detail
    r = await call(app, "DELETE", "/sessions/sess_mine/grants/agent-c",
                   headers=auth_b)
    assert r.status_code == 204
    r = await call(app, "GET", "/sessions", headers=auth_c)
    assert r.json() == []
    r = await call(app, "GET", "/sessions/sess_mine", headers=auth_c)
    assert r.status_code == 403
    # a non-creator (agent-c) attempting to grant → 403
    r = await call(app, "POST", "/sessions/sess_mine/grants", headers=auth_c,
                   json={"agent_id": "agent-d", "level": "read"})
    assert r.status_code == 403
    # unknown level → 422
    r = await call(app, "POST", "/sessions/sess_mine/grants", headers=auth_b,
                   json={"agent_id": "agent-c", "level": "owner"})
    assert r.status_code == 422


async def test_thread_lookup_respects_acl(adm_hub):
    app = adm_hub
    _, tok_b = await app.state.tokens.issue("agent-b")
    row = await _acl_seed(app, id="sess_other", agent_id="agent-c",
                          creator="agent-c")
    async with app.state.sessions._maker() as s:
        await s.execute(update(SessionRow)
                        .where(SessionRow.id == row.id)
                        .values(thread_id=555))
        await s.commit()
    # agent-b cannot read another agent's session through a thread id
    r = await call(app, "GET", "/sessions", params={"thread_id": 555},
                   headers={"authorization": f"Bearer {tok_b}"})
    assert r.status_code == 403
    # admin still resolves the binding
    r = await call(app, "GET", "/sessions", params={"thread_id": 555},
                   headers={"authorization": "Bearer adm"})
    assert r.status_code == 200
    assert r.json()["id"] == "sess_other"


# --- agent principals: action endpoints + agent-callable open --------------


def _seed_agent(app, agent_id):
    """Insert the AgentRow before the app's loop starts. StaticPool shares
    ONE sqlite connection across sessions, so a session closing mid-flight
    (any concurrent REST op) rolls back register()'s own commit — a row
    committed cleanly up front is the only stable fixture."""
    async def go():
        async with app.state.registry._maker() as s:
            s.add(AgentRow(id=agent_id, name=agent_id))
            await s.commit()
    asyncio.run(go())


def test_agent_message_send_and_read_under_acl():
    """Full prose scenario: agent-c online over a live ws; a session on
    agent-c created by agent-b through the agent-callable open; agent-a
    holds no grant. Pins write/read ACL on messages, run visibility, and
    cancel."""
    app = create_app(Settings(admin_token="adm", sqlite_path=":memory:"))
    with TestClient(app) as c:
        _seed_agent(app, "agent-c")  # after lifespan: schema exists
        with c.websocket_connect(
                "/ws/agent",
                headers={"authorization": f"Bearer {_mint(c, 'agent-c')}"}
        ) as raw_c:
            ws_c = _Socket(raw_c, reactions=_worker())
            ws_c.send(_register("agent-c", ["chat"]))
            _wait_online(app, "agent-c")
            auth_a = {"authorization": f"Bearer {_mint(c, 'agent-a')}"}
            auth_b = {"authorization": f"Bearer {_mint(c, 'agent-b')}"}
            # agent-callable POST /sessions: caller becomes creator_agent_id,
            # the row is unowned, description is mandatory
            r = c.post("/sessions", headers=auth_b,
                       json={"agent_id": "agent-c",
                             "description": "plan the sprint"})
            assert r.status_code == 201, r.text
            sess = r.json()
            assert sess["creator_agent_id"] == "agent-b"
            assert sess["owner_id"] is None
            assert sess["description"] == "plan the sprint"
            sid = sess["id"]
            assert ws_c.recv()["type"] == "session.create"

            # agent-a holds no grant: every action on the session is forbidden
            assert c.post(f"/sessions/{sid}/messages", headers=auth_a,
                          json={"text": "hi"}).status_code == 403
            assert c.get(f"/sessions/{sid}/messages",
                         headers=auth_a).status_code == 403
            assert c.get(f"/sessions/{sid}/run",
                         headers=auth_a).status_code == 403
            assert c.post(f"/sessions/{sid}/cancel",
                          headers=auth_a).status_code == 403

            # creator sends a prompt end-to-end (agent-c is online): the hub
            # records the user message and the run goes inflight
            r = c.post(f"/sessions/{sid}/messages", headers=auth_b,
                       json={"text": "hi"})
            assert r.status_code == 200 and "request_id" in r.json()
            assert ws_c.recv()["type"] == "session.message"
            r = c.get(f"/sessions/{sid}/messages", headers=auth_b)
            assert r.status_code == 200
            assert r.json()[0]["role"] == "user"
            assert r.json()[0]["content"] == "hi"
            run = c.get(f"/sessions/{sid}/run", headers=auth_b).json()
            assert run["inflight"] is True
            # creator cancels under write ACL (agent-c is a legacy adapter:
            # fire-and-forget cancel → True)
            assert c.post(f"/sessions/{sid}/cancel",
                          headers=auth_b).json() == {"cancelled": True}


async def test_agent_open_session_rejections(adm_hub):
    """Agent-callable POST /sessions: self-target, unknown target, and a
    blank description are rejected before any RPC; the admin contract
    (owner_id mandatory, offline → 409) is unchanged."""
    app = adm_hub
    _, tok_b = await app.state.tokens.issue("agent-b")
    auth_b = {"authorization": f"Bearer {tok_b}"}
    async with app.state.registry._maker() as s:
        s.add(AgentRow(id="agent-c", name="agent-c"))
        await s.commit()
    # self-target
    r = await call(app, "POST", "/sessions", headers=auth_b,
                   json={"agent_id": "agent-b", "description": "x"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "self"
    # unknown target agent
    r = await call(app, "POST", "/sessions", headers=auth_b,
                   json={"agent_id": "agent-zzz", "description": "x"})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "not_found"
    # blank and missing description
    r = await call(app, "POST", "/sessions", headers=auth_b,
                   json={"agent_id": "agent-c", "description": "   "})
    assert r.status_code == 422
    r = await call(app, "POST", "/sessions", headers=auth_b,
                   json={"agent_id": "agent-c"})
    assert r.status_code == 422
    # admin: owner_id stays mandatory, and the RPC path is untouched
    r = await call(app, "POST", "/sessions",
                   headers={"authorization": "Bearer adm"},
                   json={"agent_id": "agent-c", "description": "x"})
    assert r.status_code == 422
    r = await call(app, "POST", "/sessions",
                   headers={"authorization": "Bearer adm"},
                   json={"agent_id": "agent-c", "owner_id": "discord:1"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "agent_offline"
