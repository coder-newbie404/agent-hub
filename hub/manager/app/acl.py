"""Mechanical session ACL: creator + explicit grants. Admin is handled at
the route layer (principal None) — this module answers agent-principal
questions only."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import SessionGrantRow, SessionRow

GRANT_LEVELS = ("read", "write")


async def _grants(maker, session_id: str) -> list[SessionGrantRow]:
    async with maker() as s:
        return (await s.execute(select(SessionGrantRow)
                                .where(SessionGrantRow.session_id
                                       == session_id))).scalars().all()


async def can_write(maker: async_sessionmaker[AsyncSession], principal: str,
                    row: SessionRow) -> bool:
    if row.creator_agent_id == principal:
        return True
    for g in await _grants(maker, row.id):
        if g.agent_id == principal and g.level == "write":
            return True
    return False


async def can_read(maker: async_sessionmaker[AsyncSession], principal: str,
                   row: SessionRow) -> bool:
    # RULING: host implicit read — the hosted agent produced this journal;
    # invisibility is about *other* agents, not the host itself.
    if row.agent_id == principal:
        return True
    if await can_write(maker, principal, row):
        return True
    for g in await _grants(maker, row.id):
        if g.agent_id == principal:
            return True
    return False


async def visible_sessions(maker: async_sessionmaker[AsyncSession],
                           principal: str) -> list[SessionRow]:
    """hosted ∪ created ∪ granted, ordered by created_at."""
    granted = select(SessionGrantRow.session_id).where(
        SessionGrantRow.agent_id == principal)
    async with maker() as s:
        return list((await s.execute(
            select(SessionRow).where(
                (SessionRow.agent_id == principal)
                | (SessionRow.creator_agent_id == principal)
                | SessionRow.id.in_(granted))
            .order_by(SessionRow.created_at))).scalars().all())


async def add_grant(maker, session_id: str, agent_id: str, level: str,
                    granted_by: str) -> None:
    async with maker() as s:
        row = await s.get(SessionGrantRow, (session_id, agent_id))
        if row is None:
            row = SessionGrantRow(session_id=session_id, agent_id=agent_id,
                                  level=level, granted_by=granted_by)
            s.add(row)
        else:
            row.level = level
        await s.commit()


async def remove_grant(maker, session_id: str, agent_id: str) -> None:
    async with maker() as s:
        row = await s.get(SessionGrantRow, (session_id, agent_id))
        if row is not None:
            await s.delete(row)
            await s.commit()
