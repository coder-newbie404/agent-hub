import logging

import anyio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.protocol import (AGENT_HEARTBEAT, AGENT_MESSAGE, AGENT_REGISTER,
                          DELEGATE_MESSAGE, SESSIONS_SNAPSHOT,
                          SESSION_ACTIVITY, SESSION_COMPLETED,
                          SESSION_DELTA, SESSION_FAILED, SESSION_WAITING,
                          SYNC_ACK, SYNC_MESSAGES, SYNC_NEED, SYNC_PUSH,
                          PROTO, Envelope)
from app.tokens import bearer_secret

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/agent")
async def ws_agent(websocket: WebSocket):
    settings = websocket.app.state.settings
    registry = websocket.app.state.registry
    sessions = websocket.app.state.sessions
    token_row = await websocket.app.state.tokens.verify(
        bearer_secret(websocket.headers.get("authorization", "")))
    if token_row is None:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        env = Envelope.model_validate(await websocket.receive_json())
        if env.type != AGENT_REGISTER or not env.payload.get("agent_id"):
            await websocket.close(code=1008)
            return
        agent_id = env.payload["agent_id"]
        if token_row.agent_id is not None and agent_id != token_row.agent_id:
            # a token is pinned to its agent; one leak must not register
            # another identity
            logger.warning("token %s tried agent %s but is pinned to %s",
                           token_row.token_id, agent_id, token_row.agent_id)
            await websocket.close(code=1008)
            return
        if env.protocol_version != PROTO:
            logger.warning("refusing agent %s: protocol version %s != %s",
                           env.payload.get("agent_id"), env.protocol_version,
                           PROTO)
            await websocket.close(code=1008)
            return
        conn = await registry.register(
            agent_id, env.payload.get("name", agent_id),
            env.payload.get("capabilities", []), websocket,
            description=str(env.payload.get("description", "")),
            tools=env.payload.get("tools") or [],
            environment=env.payload.get("environment") or {})
        # locally-owning adapters run the hub in projection mode
        registry.set_projection(
            conn.agent_id,
            "owns_sessions" in env.payload.get("capabilities", []))
        # a reconnecting projection adapter learns its cursor immediately,
        # without waiting for its first sync.push round-trip
        if "owns_sessions" in env.payload.get("capabilities", []):
            sync = websocket.app.state.sync
            await conn.send(Envelope(type=SYNC_ACK,
                                     payload=await sync.ack_payload(
                                         conn.agent_id)))
    except WebSocketDisconnect:
        return  # never registered → nothing to mark offline
    try:
        while True:
            env = Envelope.model_validate(await websocket.receive_json())
            if env.type == AGENT_HEARTBEAT:
                await registry.touch(conn.agent_id)
                continue
            if conn.resolve(env):
                continue
            if env.type == SESSION_WAITING:
                await sessions.on_waiting(env.session_id, env.payload.get("state", ""))
            elif env.type == AGENT_MESSAGE:
                # the delegation broker wants the last reply text before the
                # message lands in the transcript
                websocket.app.state.delegation.note_reply(
                    env.session_id, env.payload.get("text", ""))
                await sessions.on_agent_message(env.session_id,
                                                env.payload.get("text", ""),
                                                env.request_id)
            elif env.type == SESSION_DELTA:
                await sessions.on_session_delta(env.session_id,
                                                env.payload.get("text"),
                                                bool(env.payload.get("reset")))
            elif env.type == SESSIONS_SNAPSHOT:
                await sessions.reconcile_snapshot(
                    conn.agent_id, env.payload.get("sessions", []),
                    bool(env.payload.get("done")))
            elif env.type == SESSION_ACTIVITY:
                await sessions.on_session_activity(env.session_id, env.payload)
            elif env.type == SESSION_COMPLETED:
                await sessions.on_completed(env.session_id,
                                            env.payload.get("omp_session_id"),
                                            env.payload)
            elif env.type == SESSION_FAILED:
                await sessions.on_failed(env.session_id,
                                         env.payload.get("reason", "error"))
            elif env.type == SYNC_PUSH:
                result = await websocket.app.state.sync.apply(
                    conn.agent_id, str(env.payload.get("instance", "")),
                    int(env.payload.get("cursor_start", 0)),
                    list(env.payload.get("events", [])))
                await conn.send(Envelope(type=SYNC_ACK, payload=result))
            elif env.type == SYNC_NEED:
                result = await websocket.app.state.sync.respond_need(
                    conn.agent_id,
                    str(env.payload.get("omp_session_id", "")),
                    int(env.payload.get("after_message_id", 0)))
                await conn.send(Envelope(type=SYNC_MESSAGES,
                                         session_id=env.session_id,
                                         payload=result))
            elif env.type == DELEGATE_MESSAGE:
                await websocket.app.state.delegation.on_delegate(conn, env)
            else:
                logger.debug("unhandled %s", env.type)
    except WebSocketDisconnect:
        logger.debug("agent %s disconnected", conn.agent_id)
    finally:
        # ANY exit of the receive loop (disconnect, malformed frame, handler
        # error) must take the agent offline; sweep is only the backstop.
        # The identity check stops a stale loop from closing a connection
        # that re-registered after a slow unwind.
        # Shielded: this unwind must complete even when the receive task is
        # being cancelled (server shutdown, test portal teardown). A cancelled
        # unwind would both strand the agent online AND abort SQLAlchemy
        # mid-statement, which hard-invalidates the pooled connection.
        with anyio.CancelScope(shield=True):
            await registry.mark_offline_if_current(conn)
