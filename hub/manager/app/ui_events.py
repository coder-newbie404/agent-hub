"""Hub → frontend event broker.

The Discord frontend subscribes at /ws/ui and receives every session/sync
event the in-process bot used to get through the notifier callback slot.
Frames are plain JSON dicts (push-only channel; the frontend consumes them
directly, no Envelope round-trip)."""
import logging

logger = logging.getLogger(__name__)


class UiBroker:
    """Implements the SessionNotifier protocol and the sync `on_applied`
    hook; every attached UI websocket gets each event as one JSON frame."""

    def __init__(self):
        self._clients: set = set()

    async def attach(self, ws) -> None:
        """Register `ws` and hold it until the client goes away. Relies on
        Starlette's `receive()` (WebSocket is not async-iterable)."""
        self._clients.add(ws)
        try:
            while (await ws.receive())["type"] != "websocket.disconnect":
                pass  # push-only: drain and ignore client frames
        except Exception:
            logger.debug("ui client detached", exc_info=True)
        finally:
            self._clients.discard(ws)

    async def _broadcast(self, frame: dict) -> None:
        for ws in list(self._clients):
            try:
                await ws.send_json(frame)
            except Exception:
                self._clients.discard(ws)

    async def on_message(self, session_id: str, text: str,
                         message_id: int) -> None:
        await self._broadcast({"type": "session.message",
                               "session_id": session_id, "text": text,
                               "message_id": message_id})

    async def on_delta(self, session_id: str, draft: str) -> None:
        await self._broadcast({"type": "session.delta",
                               "session_id": session_id, "draft": draft})

    async def on_activity(self, session_id: str, label: str | None) -> None:
        await self._broadcast({"type": "session.activity",
                               "session_id": session_id, "label": label})

    async def on_completed(self, session_id: str) -> None:
        await self._broadcast({"type": "session.completed",
                               "session_id": session_id})

    async def on_failed(self, session_id: str, reason: str) -> None:
        await self._broadcast({"type": "session.failed",
                               "session_id": session_id, "reason": reason})

    async def on_agent_disconnected(self, agent_id: str) -> None:
        await self._broadcast({"type": "agent.disconnected",
                               "agent_id": agent_id})

    async def on_sync_applied(self, agent_id: str,
                              session_ids: list[str]) -> None:
        await self._broadcast({"type": "sync.applied", "agent_id": agent_id,
                               "session_ids": session_ids})
