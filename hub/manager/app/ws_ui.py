"""UI event channel: the frontend (Discord bot) subscribes here for
session/sync events; everything else it needs crosses the REST API."""
import logging

from fastapi import APIRouter, WebSocket

from app.tokens import bearer_secret

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/ui")
async def ws_ui(websocket: WebSocket):
    token_row = await websocket.app.state.tokens.verify(
        bearer_secret(websocket.headers.get("authorization", "")))
    # admin-only: agent_id None is the bootstrap/admin row (a decision made
    # here, not routes.require_admin, which compares the env secret
    # directly); the broker fans out every session's frames
    if token_row is None or token_row.agent_id is not None:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    await websocket.app.state.ui_broker.attach(websocket)
