from datetime import datetime, timezone

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    # Naive UTC: SQLite DATETIME processors drop tzinfo, so aware-in/naive-out
    # would mix kinds. Keep the explicit UTC source, strip the offset.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(AsyncAttrs, DeclarativeBase):
    pass


class AgentRow(Base):
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="offline")
    capabilities: Mapped[str] = mapped_column(Text, default="[]")
    description: Mapped[str] = mapped_column(Text, default="")
    tools: Mapped[str] = mapped_column(Text, default="[]")
    environment: Mapped[str] = mapped_column(Text, default="{}")
    last_seen: Mapped[datetime] = mapped_column(default=utcnow)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class SessionRow(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"))
    owner_id: Mapped[str | None] = mapped_column(String(64))
    thread_id: Mapped[int | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(String(16), default="created")
    workspace: Mapped[str | None] = mapped_column(String(1024), default=None)
    title: Mapped[str | None] = mapped_column(String(128), default=None)
    archived: Mapped[bool] = mapped_column(default=False)
    omp_session_id: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    creator_agent_id: Mapped[str | None] = mapped_column(String(64), default=None)
    description: Mapped[str] = mapped_column(Text, default="")
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)


class SessionGrantRow(Base):
    __tablename__ = "session_grants"
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id"), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # "read" | "write"
    level: Mapped[str] = mapped_column(String(8))
    granted_by: Mapped[str] = mapped_column(String(64))
    granted_at: Mapped[datetime] = mapped_column(default=utcnow)


class MessageRow(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"))
    request_id: Mapped[str | None] = mapped_column(String(32), default=None)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # NULL = never rendered in a Discord thread (drives missed-message sync)
    posted_at: Mapped[datetime | None] = mapped_column(default=None)


class SyncStateRow(Base):
    __tablename__ = "sync_state"
    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    instance: Mapped[str] = mapped_column(String(64), default="")
    cursor: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow,
                                                 onupdate=utcnow)


class AgentTokenRow(Base):
    __tablename__ = "agent_tokens"
    token_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # NULL = bootstrap token, valid for registering any agent id until
    # superseded; per-agent tokens pin the identity to the agent_id.
    agent_id: Mapped[str | None] = mapped_column(String(64), default=None)
    # sha-256 hex of the bearer secret; the plaintext is shown once at mint
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
