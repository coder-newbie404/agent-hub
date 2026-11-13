"""Hub-side sync coordinator: applies the adapter's journal tail to the
projection DB with exactly-once-per-offset semantics, and answers with a
cumulative ack + message catalog (the mirror path's driver)."""
import logging

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import MessageRow, SessionRow, SyncStateRow, utcnow

from app.sessions import resolve_local_row, upsert_projected

logger = logging.getLogger(__name__)

# sync.messages batch cap (mirrors SYNC_BATCH on the push path)
NEED_BATCH = 200


class SyncManager:
    def __init__(self, maker: async_sessionmaker[AsyncSession],
                 on_applied=None):
        self._maker = maker
        self.on_applied = on_applied  # async (agent_id, session_ids)

    async def stored(self, agent_id: str) -> tuple[str, int]:
        async with self._maker() as s:
            row = await s.get(SyncStateRow, agent_id)
        return (row.instance, row.cursor) if row else ("", 0)

    async def catalog(self, agent_id: str) -> list[dict]:
        async with self._maker() as s:
            rows = (await s.execute(
                select(SessionRow).where(
                    SessionRow.agent_id == agent_id,
                    # rows the adapter has not confirmed (NULL omp id: a
                    # hub-created session whose create never landed) are
                    # hub-internal — advertising them makes the adapter
                    # adopt ghost local sessions keyed by hub ids
                    SessionRow.omp_session_id.is_not(None)))).scalars().all()
            out = []
            for r in rows:
                cnt, last = (await s.execute(
                    select(func.count(MessageRow.id),
                           func.max(MessageRow.id))
                    .where(MessageRow.session_id == r.id))).one()
                out.append({"omp_session_id": r.omp_session_id,
                            "hub_session_id": r.id,
                            "message_count": cnt or 0,
                            "last_message_id": last or 0})
            return out

    async def ack_payload(self, agent_id: str) -> dict:
        instance, cursor = await self.stored(agent_id)
        return {"instance": instance, "cursor": cursor,
                "catalog": await self.catalog(agent_id)}

    async def respond_need(self, agent_id: str, omp_session_id: str,
                           after_message_id: int) -> dict:
        """Answer the adapter's sync.need: the hub→local mirror's pull
        path. Unknown session → empty done batch (nothing to merge)."""
        row = await resolve_local_row(self._maker, agent_id, omp_session_id)
        if row is None:
            return {"omp_session_id": omp_session_id,
                    "hub_session_id": None, "messages": [], "done": True}
        async with self._maker() as s:
            rows = (await s.execute(
                select(MessageRow)
                .where(MessageRow.session_id == row.id,
                       MessageRow.id > after_message_id)
                .order_by(MessageRow.id)
                .limit(NEED_BATCH))).scalars().all()
        return {"omp_session_id": omp_session_id,
                "hub_session_id": row.id,
                "messages": [{"id": m.id, "role": m.role,
                              "content": m.content,
                              "created_at": m.created_at.isoformat()
                              if m.created_at else None,
                              "request_id": m.request_id}
                             for m in rows],
                "done": len(rows) < NEED_BATCH}

    async def apply(self, agent_id: str, instance: str, cursor_start: int,
                    events: list[dict]) -> dict:
        stored_instance, stored_cursor = await self.stored(agent_id)
        adopted = instance != stored_instance
        if adopted:
            # hub state lost/replaced (fresh DB): adopt the journal from
            # scratch — mirrored rows are restored, not skipped (they are
            # this DB's only copy of messages it once held)
            stored_instance, stored_cursor = instance, 0
        if cursor_start != stored_cursor:
            # gap: report what we have; the adapter rewinds and resends
            ack = await self.ack_payload(agent_id)
            ack["applied"] = 0
            return ack
        touched: dict[str, str] = {}  # local_id -> hub session id
        applied = 0
        for ev in events:
            kind = ev.get("kind")
            local_id = ev.get("local_id") or ""
            if not local_id:
                continue
            if kind == "session_upserted":
                row = await upsert_projected(
                    self._maker, agent_id, local_id,
                    status=ev.get("status") or "idle",
                    workspace=ev.get("ws"), omp=ev.get("omp"))
                touched[local_id] = row.id
                applied += 1
            elif kind == "session_titled":
                # the adapter mirroring the local omp name in; ride on the
                # existing row (stub only if the hub lost its DB — the
                # replayed session_upserted normally precedes this)
                title = str(ev.get("title") or "").strip()
                if title:
                    row = await self._resolve_or_stub(agent_id, local_id)
                    touched.setdefault(local_id, row.id)
                    async with self._maker() as s:
                        await s.execute(update(SessionRow)
                                        .where(SessionRow.id == row.id)
                                        .values(title=title))
                        await s.commit()
                    applied += 1
            elif kind == "message":
                mirrored = bool(ev.get("mirrored"))
                if mirrored and not adopted:
                    continue  # hub→local mirror: this row is the hub's own
                row = await self._resolve_or_stub(agent_id, local_id)
                touched.setdefault(local_id, row.id)
                dup = ev.get("request_id")
                if dup and await self._request_seen(dup):
                    continue  # live-recorded run: already in the DB
                created = self._parse_ts(ev.get("ts"))
                # a mirrored restore was rendered by the hub once already;
                # native events are new renders and print like always
                posted = created if mirrored else None
                async with self._maker() as s:
                    s.add(MessageRow(
                        session_id=row.id, request_id=dup,
                        role=ev.get("role") or "assistant",
                        content=ev.get("content") or "",
                        created_at=created, posted_at=posted))
                    # local chatter is activity: keep the hub picker's
                    # recent-first ordering truthful for imported rows
                    await s.execute(update(SessionRow)
                                    .where(SessionRow.id == row.id)
                                    .values(updated_at=utcnow()))
                    await s.commit()
                applied += 1
            # other kinds (run_started/run_finished/journal_started):
            # lifecycle only — the cursor advance below is what matters
        async with self._maker() as s:
            await s.merge(SyncStateRow(agent_id=agent_id,
                                       instance=instance,
                                       cursor=cursor_start + len(events)))
            await s.commit()
        if touched and self.on_applied is not None:
            try:
                await self.on_applied(agent_id,
                                      sorted(set(touched.values())))
            except Exception:
                logger.exception("sync on_applied hook failed")
        return {"instance": instance, "cursor": cursor_start + len(events),
                "applied": applied,
                "catalog": await self.catalog(agent_id)}

    async def _resolve_or_stub(self, agent_id: str, local_id: str):
        """A message for an unknown local session: import a minimal row so
        the transcript has an anchor (mirrors snapshot reconcile)."""
        return await upsert_projected(self._maker, agent_id, local_id,
                                      status="idle")

    async def _request_seen(self, request_id: str) -> bool:
        async with self._maker() as s:
            row = (await s.execute(
                select(MessageRow.id).where(
                    MessageRow.request_id == request_id))).first()
        return row is not None

    @staticmethod
    def _parse_ts(ts: str | None):
        from datetime import datetime
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts).replace(tzinfo=None)
        except ValueError:
            return None
