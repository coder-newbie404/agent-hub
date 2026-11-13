import json
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import acl
from app.protocol import SYNC_NOW, Envelope
from app.sessions import (AgentOffline, NotFound, RunInProgress,
                          SessionError)
from app.tokens import bearer_secret

router = APIRouter()


def require_admin(request: Request) -> None:
    """REST auth: the ADMIN_TOKEN env secret (also the bootstrap agent
    token). REST is owner-facing only — adapters never call it."""
    got = request.headers.get("authorization", "")
    # compare bytes: compare_digest raises TypeError on non-ASCII str
    expected = f"Bearer {request.app.state.settings.admin_token}".encode(
        "utf-8")
    if not secrets.compare_digest(got.encode("utf-8", "replace"), expected) \
            or not request.app.state.settings.admin_token:
        raise HTTPException(status_code=401, detail="unauthorized")


async def require_agent(request: Request) -> str | None:
    """Bearer → principal for agent-callable routes. None = admin
    (bootstrap token); otherwise the token's pinned agent id."""
    secret = bearer_secret(request.headers.get("authorization", ""))
    if secret and secrets.compare_digest(
            secret.encode("utf-8", "replace"),
            request.app.state.settings.admin_token.encode("utf-8", "replace")):
        return None
    row = await request.app.state.tokens.verify(secret)
    if row is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    if row.agent_id is None:
        return None
    return row.agent_id


@router.get("/health")
async def health():
    return {"status": "ok"}


def _agent_view(r, request: Request) -> dict:
    return {"id": r.id, "name": r.name, "status": r.status,
            "capabilities": r.capabilities,
            "description": r.description,
            "tools": json.loads(r.tools) if r.tools else [],
            "environment": json.loads(r.environment) if r.environment else {},
            "last_seen": (r.last_seen.isoformat() if r.last_seen
                          else None),
            "online": request.app.state.registry.online(r.id)}


@router.get("/agents")
async def agents(request: Request,
                 principal: str | None = Depends(require_agent)):
    return [_agent_view(r, request)
            for r in await request.app.state.registry.list()]


@router.get("/agents/{agent_id}")
async def agent_detail(agent_id: str, request: Request,
                       principal: str | None = Depends(require_agent)):
    for r in await request.app.state.registry.list():
        if r.id == agent_id:
            return _agent_view(r, request)
    raise _err(404, "not_found", f"unknown agent {agent_id}")


def _err(status: int, code: str, message: str) -> HTTPException:
    """Machine-readable error envelope for the frontend client."""
    return HTTPException(status_code=status,
                         detail={"code": code, "message": message})


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFound):
        return _err(404, "not_found", str(exc))
    if isinstance(exc, RunInProgress):
        return _err(409, "run_in_progress", str(exc))
    if isinstance(exc, AgentOffline):
        return _err(409, "agent_offline", str(exc))
    if isinstance(exc, SessionError):
        return _err(502, "agent_failed", str(exc))
    raise exc


HUB_ERRORS = (NotFound, RunInProgress, AgentOffline, SessionError)


def _session_view(r) -> dict:
    return {"id": r.id, "agent_id": r.agent_id, "owner_id": r.owner_id,
            "status": r.status, "workspace": r.workspace,
            "thread_id": r.thread_id, "omp_session_id": r.omp_session_id,
            "title": r.title, "archived": r.archived,
            "created_at": r.created_at.isoformat(),
            "updated_at": r.updated_at.isoformat(),
            "creator_agent_id": r.creator_agent_id,
            "description": r.description,
            "last_used_at": (r.last_used_at.isoformat() if r.last_used_at
                             else None),
            "lifecycle": ("archived" if r.archived
                          else ("active"
                                if r.status in ("running", "waiting")
                                else r.status))}


def _message_view(m) -> dict:
    return {"id": m.id, "role": m.role, "content": m.content,
            "created_at": m.created_at.isoformat(),
            "posted_at": m.posted_at.isoformat() if m.posted_at else None}


async def _acl_row(mgr, session_id: str, principal: str | None, *,
                   write: bool) -> None:
    """Agent principals act on a session only through its ACL; admin
    (principal None) stays owner-facing and unscoped."""
    if principal is None:
        return
    try:
        row = await mgr.get(session_id)
    except HUB_ERRORS as exc:
        raise _translate(exc)
    check = acl.can_write if write else acl.can_read
    if not await check(mgr._maker, principal, row):
        raise HTTPException(status_code=403, detail="forbidden")


@router.get("/sessions/missed", dependencies=[Depends(require_admin)])
async def missed_sessions(request: Request):
    """Sessions with messages that were never rendered in Discord."""
    return {"session_ids": await request.app.state.sessions.missed_session_ids()}


@router.post("/sessions/recover-interrupted",
             dependencies=[Depends(require_admin)])
async def recover_interrupted(request: Request):
    """Startup recovery: RUNNING/WAITING rows flip to FAILED, ids returned."""
    return {"session_ids": await request.app.state.sessions.recover_interrupted()}


class OpenSessionBody(BaseModel):
    agent_id: str
    owner_id: str | None = None  # admin path; agent-created rows are unowned
    workspace: str | None = None
    title: str | None = None
    description: str | None = None  # required for agent-created sessions


@router.post("/sessions", status_code=201)
async def open_session(body: OpenSessionBody, request: Request,
                       principal: str | None = Depends(require_agent)):
    """Create a session through the agent (sync RPC; 409 when offline)."""
    if principal is None:
        if body.owner_id is None:
            raise HTTPException(status_code=422, detail="owner_id required")
        try:
            row = await request.app.state.sessions.open_session(
                body.agent_id, body.owner_id, body.workspace, body.title)
        except HUB_ERRORS as exc:
            raise _translate(exc)
        return _session_view(row)
    # agent principal: the caller becomes the creator, the row is unowned,
    # and the target must be another registered agent with a real description
    if body.agent_id == principal:
        raise _err(422, "self", "cannot open a session on yourself")
    if not any(r.id == body.agent_id
               for r in await request.app.state.registry.list()):
        raise _err(404, "not_found", f"unknown agent {body.agent_id}")
    if not (body.description or "").strip():
        raise _err(422, "description_required", "description is required")
    try:
        row = await request.app.state.sessions.open_session(
            body.agent_id, None, body.workspace, body.title,
            creator_agent_id=principal, description=body.description)
    except HUB_ERRORS as exc:
        raise _translate(exc)
    return _session_view(row)


@router.get("/sessions")
async def sessions(request: Request, thread_id: int | None = None,
                   principal: str | None = Depends(require_agent)):
    mgr = request.app.state.sessions
    if thread_id is not None:
        row = await mgr.by_thread(thread_id)
        if row is None:
            raise _err(404, "not_found",
                       f"no session bound to thread {thread_id}")
        if principal is not None and not await acl.can_read(
                mgr._maker, principal, row):
            raise HTTPException(status_code=403, detail="forbidden")
        return _session_view(row)
    if principal is None:
        rows = await mgr.list()
    else:
        rows = await acl.visible_sessions(mgr._maker, principal)
    return [_session_view(r) for r in rows]


@router.get("/sessions/{session_id}")
async def session_detail(session_id: str, request: Request,
                         principal: str | None = Depends(require_agent)):
    try:
        row = await request.app.state.sessions.get(session_id)
    except HUB_ERRORS as exc:
        raise _translate(exc)
    if principal is not None and not await acl.can_read(
            request.app.state.sessions._maker, principal, row):
        raise HTTPException(status_code=403, detail="forbidden")
    return _session_view(row)


class GrantBody(BaseModel):
    agent_id: str
    level: str  # "read" | "write"


@router.post("/sessions/{session_id}/grants", status_code=201)
async def create_grant(session_id: str, body: GrantBody, request: Request,
                       principal: str | None = Depends(require_agent)):
    mgr = request.app.state.sessions
    try:
        row = await mgr.get(session_id)
    except HUB_ERRORS as exc:
        raise _translate(exc)
    if principal is not None and row.creator_agent_id != principal:
        raise HTTPException(status_code=403, detail="not the creator")
    if body.level not in acl.GRANT_LEVELS:
        raise HTTPException(status_code=422, detail="level must be read|write")
    await acl.add_grant(mgr._maker, session_id, body.agent_id, body.level,
                        principal or "admin")
    return {"session_id": session_id, "agent_id": body.agent_id,
            "level": body.level}


@router.delete("/sessions/{session_id}/grants/{agent_id}", status_code=204)
async def delete_grant(session_id: str, agent_id: str, request: Request,
                       principal: str | None = Depends(require_agent)):
    mgr = request.app.state.sessions
    try:
        row = await mgr.get(session_id)
    except HUB_ERRORS as exc:
        raise _translate(exc)
    if principal is not None and row.creator_agent_id != principal:
        raise HTTPException(status_code=403, detail="not the creator")
    await acl.remove_grant(mgr._maker, session_id, agent_id)


class SessionPatch(BaseModel):
    title: str | None = None
    archived: bool | None = None
    thread_id: int | None = None


@router.patch("/sessions/{session_id}", dependencies=[Depends(require_admin)])
async def session_patch(session_id: str, body: SessionPatch,
                        request: Request):
    mgr = request.app.state.sessions
    try:
        if body.title is not None:
            await mgr.rename(session_id, body.title)
        if body.archived is not None:
            await mgr.set_archived(session_id, body.archived)
        if body.thread_id is not None:
            await mgr.attach_thread(session_id, body.thread_id)
        return _session_view(await mgr.get(session_id))
    except HUB_ERRORS as exc:
        raise _translate(exc)


@router.delete("/sessions/{session_id}", status_code=204,
               dependencies=[Depends(require_admin)])
async def session_delete(session_id: str, request: Request):
    try:
        await request.app.state.sessions.delete(session_id)
    except HUB_ERRORS as exc:
        raise _translate(exc)


class MessageBody(BaseModel):
    text: str


@router.post("/sessions/{session_id}/messages")
async def post_message(session_id: str, body: MessageBody, request: Request,
                       principal: str | None = Depends(require_agent)):
    """Send a user prompt to the agent (409 offline / run-in-progress)."""
    mgr = request.app.state.sessions
    await _acl_row(mgr, session_id, principal, write=True)
    try:
        request_id = await mgr.user_message(session_id, body.text)
    except HUB_ERRORS as exc:
        raise _translate(exc)
    return {"request_id": request_id}


@router.get("/sessions/{session_id}/messages")
async def session_messages(session_id: str, request: Request,
                           unposted: bool = False,
                           principal: str | None = Depends(require_agent)):
    mgr = request.app.state.sessions
    await _acl_row(mgr, session_id, principal, write=False)
    try:
        rows = (await mgr.missed_messages(session_id) if unposted
                else await mgr.messages(session_id))
    except HUB_ERRORS as exc:
        raise _translate(exc)
    return [_message_view(m) for m in rows]


class MarkPostedBody(BaseModel):
    ids: list[int]


@router.post("/sessions/{session_id}/messages/mark-posted",
             dependencies=[Depends(require_admin)])
async def mark_posted(session_id: str, body: MarkPostedBody,
                      request: Request):
    await request.app.state.sessions.mark_posted(body.ids)
    return {"marked": len(body.ids)}


@router.get("/sessions/{session_id}/messages/count",
            dependencies=[Depends(require_admin)])
async def message_count(session_id: str, request: Request):
    return {"count": await request.app.state.sessions.message_count(session_id)}


@router.get("/sessions/{session_id}/run")
async def run_status(session_id: str, request: Request,
                     principal: str | None = Depends(require_agent)):
    mgr = request.app.state.sessions
    await _acl_row(mgr, session_id, principal, write=False)
    return {"inflight": session_id in mgr.inflight,
            "activity": mgr.activity.get(session_id)}


@router.post("/sessions/{session_id}/cancel")
async def cancel_session(session_id: str, request: Request,
                         principal: str | None = Depends(require_agent)):
    mgr = request.app.state.sessions
    await _acl_row(mgr, session_id, principal, write=True)
    try:
        cancelled = await mgr.cancel(session_id)
    except HUB_ERRORS as exc:
        raise _translate(exc)
    return {"cancelled": cancelled}


@router.post("/sync/nudge", dependencies=[Depends(require_admin)])
async def sync_nudge(request: Request):
    """Push sync.now to every online locally-owning agent (the /sync command)."""
    registry = request.app.state.registry
    nudged = 0
    for agent_id in registry.projection_agents():
        conn = registry.get(agent_id)
        if conn is None:
            continue
        try:
            await conn.send(Envelope(type=SYNC_NOW, payload={}))
        except Exception:
            continue
        nudged += 1
    return {"nudged": nudged}


# --- per-agent tokens ---------------------------------------------------

class MintBody(BaseModel):
    agent_id: str
    ttl_days: int | None = None  # None → settings.token_ttl_days at service level


def _token_view(r) -> dict:
    return {"token_id": r.token_id, "agent_id": r.agent_id,
            "created_at": (r.created_at.isoformat() if r.created_at
                           else None),
            "expires_at": (r.expires_at.isoformat() if r.expires_at
                           else None),
            "last_used_at": (r.last_used_at.isoformat() if r.last_used_at
                             else None),
            "revoked": r.revoked_at is not None}


@router.post("/tokens", dependencies=[Depends(require_admin)])
async def mint_token(body: MintBody, request: Request):
    ttl = body.ttl_days
    if ttl is None:
        ttl = request.app.state.settings.token_ttl_days
    row, secret = await request.app.state.tokens.issue(body.agent_id, ttl)
    return {"token": secret, **_token_view(row)}


@router.get("/tokens", dependencies=[Depends(require_admin)])
async def list_tokens(request: Request):
    return [_token_view(r) for r in await request.app.state.tokens.list()]


@router.post("/tokens/{agent_id}/rotate", dependencies=[Depends(require_admin)])
async def rotate_token(agent_id: str, request: Request):
    await _disconnect_agent(request, agent_id)
    row, secret = await request.app.state.tokens.rotate(
        agent_id, request.app.state.settings.token_ttl_days)
    return {"token": secret, **_token_view(row)}


@router.post("/tokens/{agent_id}/revoke", dependencies=[Depends(require_admin)])
async def revoke_token(agent_id: str, request: Request):
    await _disconnect_agent(request, agent_id)
    n = await request.app.state.tokens.revoke(agent_id=agent_id)
    return {"revoked": n}


async def _disconnect_agent(request: Request, agent_id: str) -> None:
    """Rotation/revocation must drop any live session that still rides on
    the old secret; the WS loop marks the agent offline on disconnect."""
    conn = request.app.state.registry.get(agent_id)
    if conn is not None:
        await conn.close()
