"""Protocol v1 envelope — verbatim mirror of manager/app/protocol.py (JSON is the contract)."""
import uuid
from typing import Any

from pydantic import BaseModel, Field

PROTO = 1

AGENT_REGISTER = "agent.register"
AGENT_HEARTBEAT = "agent.heartbeat"
AGENT_DISCONNECT = "agent.disconnect"
SESSION_CREATE = "session.create"
SESSION_MESSAGE = "session.message"
SESSION_CANCEL = "session.cancel"
SESSION_CANCEL_ACK = "session.cancel_ack"
SESSIONS_SNAPSHOT = "sessions.snapshot"
SESSION_WAITING = "session.waiting"
AGENT_MESSAGE = "agent.message"
SESSION_DELTA = "session.delta"
SESSION_ACTIVITY = "session.activity"
SESSION_COMPLETED = "session.completed"
SESSION_FAILED = "session.failed"
SYNC_PUSH = "sync.push"
SYNC_ACK = "sync.ack"
SYNC_NOW = "sync.now"
SYNC_NEED = "sync.need"
SYNC_MESSAGES = "sync.messages"
DELEGATE_MESSAGE = "delegate.message"
DELEGATION_RESULT = "delegation.result"
DELEGATION_EVENT = "delegation.event"  # reserved: live relay, not sent in v1


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Envelope(BaseModel):
    protocol_version: int = PROTO
    type: str
    request_id: str | None = None
    session_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
