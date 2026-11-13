"""Per-agent bearer tokens: hashing, expiry, rotation, revocation.

The hub stores only sha-256 hashes; the plaintext secret is returned once
at mint time and shown to the owner. A bootstrap token (agent_id NULL) is
seeded from the ADMIN_TOKEN env var so an existing deployment keeps working
until per-agent tokens replace it.
"""
import hashlib
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AgentTokenRow, utcnow

SECRET_PREFIX = "ask_"
TOKEN_PREFIX = "tok_"


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8", "replace")).hexdigest()


def bearer_secret(header_value: str) -> str:
    """Bearer-secret extraction from an Authorization header value.

    Header bytes can hold non-ASCII (scanners); 'replace' in hash_secret
    keeps the hash total so a bogus value is rejected as unknown instead
    of raising."""
    raw = header_value.strip()
    if raw.startswith("Bearer "):
        raw = raw[len("Bearer "):]
    return raw


class TokenService:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def issue(self, agent_id: str | None = None,
                    ttl_days: int | None = 30) -> tuple[AgentTokenRow, str]:
        """Mint a token; the plaintext secret is the second element."""
        if ttl_days is not None and ttl_days <= 0:
            raise ValueError("ttl_days must be positive or None")
        secret = SECRET_PREFIX + secrets.token_urlsafe(32)
        row = AgentTokenRow(
            token_id=TOKEN_PREFIX + uuid.uuid4().hex[:12],
            agent_id=agent_id or None,
            token_hash=hash_secret(secret),
        )
        if ttl_days is not None:
            row.expires_at = utcnow() + timedelta(days=ttl_days)
        async with self._maker() as s:
            s.add(row)
            await s.commit()
        return row, secret

    async def verify(self, secret: str) -> AgentTokenRow | None:
        """Active (never-revoked, unexpired) token for this secret, or None.

        Touches last_used_at on success; unknown/revoked/expired all return
        None without distinguishing them to the caller."""
        if not secret:
            return None
        async with self._maker() as s:
            row = await s.scalar(select(AgentTokenRow).where(
                AgentTokenRow.token_hash == hash_secret(secret)))
            if row is None or row.revoked_at is not None:
                return None
            if row.expires_at is not None and row.expires_at <= utcnow():
                return None
            row.last_used_at = utcnow()
            await s.commit()
            return row

    async def rotate(self, agent_id: str,
                     ttl_days: int | None = 30) -> tuple[AgentTokenRow, str]:
        """Revoke every active token for the agent, mint its replacement.

        Overlapping long-lived secrets are exactly what rotation must
        eliminate, so the old token is invalid the moment this returns."""
        await self.revoke(agent_id=agent_id)
        return await self.issue(agent_id, ttl_days)

    async def revoke(self, agent_id: str | None = None,
                     token_id: str | None = None) -> int:
        """Revoke active tokens matching the selector; returns count.

        Scoped to the selector only: revoking an agent must never touch
        the bootstrap wildcard (that is the owner's explicit call)."""
        async with self._maker() as s:
            q = select(AgentTokenRow).where(AgentTokenRow.revoked_at.is_(None))
            if token_id is not None:
                q = q.where(AgentTokenRow.token_id == token_id)
            elif agent_id is not None:
                q = q.where(AgentTokenRow.agent_id == agent_id)
            rows = (await s.execute(q)).scalars().all()
            for row in rows:
                row.revoked_at = utcnow()
            await s.commit()
            return len(rows)

    async def list(self) -> list[AgentTokenRow]:
        async with self._maker() as s:
            return (await s.execute(
                select(AgentTokenRow).order_by(AgentTokenRow.created_at)
            )).scalars().all()

    async def ensure_bootstrap(self, admin_secret: str) -> bool:
        """Seed/refresh the bootstrap token row from the ADMIN_TOKEN env.

        True when the table was empty (first boot on an existing env).
        Makes ADMIN_TOKEN rotation from the env stick: a stale bootstrap
        row is revoked, the new secret becomes the only wildcard. The env
        is authoritative, so a revoked row carrying the current secret is
        restored (e.g. after an over-broad revoke).
        """
        if not admin_secret:
            return False
        new_hash = hash_secret(admin_secret)
        async with self._maker() as s:
            all_rows = (await s.execute(select(AgentTokenRow))).scalars().all()
            fresh = not all_rows
            for row in all_rows:
                if row.agent_id is None and row.token_hash != new_hash \
                        and row.revoked_at is None:
                    row.revoked_at = utcnow()
            existing = await s.scalar(select(AgentTokenRow).where(
                AgentTokenRow.token_hash == new_hash))
            if existing is None:
                s.add(AgentTokenRow(
                    token_id=TOKEN_PREFIX + uuid.uuid4().hex[:12],
                    agent_id=None, token_hash=new_hash))
            elif existing.revoked_at is not None:
                existing.revoked_at = None
            await s.commit()
            return fresh
