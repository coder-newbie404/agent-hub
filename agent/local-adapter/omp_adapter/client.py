import asyncio
import json
import logging

import websockets

from omp_adapter.protocol import AGENT_HEARTBEAT, AGENT_REGISTER, Envelope, PROTO
from omp_adapter.delegation import DelegationClient, DropBox

logger = logging.getLogger(__name__)


def next_backoff(attempt: int, cap: int = 30) -> float:
    return float(min(2 ** attempt, cap))


def _safe_json(raw: str, default):
    """Parse a Settings JSON-string field; fall back on empty/malformed."""
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


class OMPClient:
    def __init__(self, settings, agent):
        self.settings = settings
        self.agent = agent
        self.ws = None
        # (session_id, request_id) -> Future; mirrors manager AgentConnection
        self._pending: dict[tuple[str | None, str | None],
                            asyncio.Future] = {}

    async def call(self, env: Envelope, timeout: float = 600.0) -> Envelope:
        if self.ws is None:
            raise ConnectionError("not connected")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[(env.session_id, env.request_id)] = fut
        try:
            await self.ws.send(env.model_dump_json())
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop((env.session_id, env.request_id), None)

    def resolve(self, env: Envelope) -> bool:
        fut = self._pending.pop((env.session_id, env.request_id), None)
        if fut is not None and not fut.done():
            fut.set_result(env)
            return True
        return False

    async def session(self, ws) -> None:
        self.ws = ws
        await ws.send(json.dumps({
            "protocol_version": PROTO, "type": AGENT_REGISTER,
            "payload": {"agent_id": self.settings.agent_id,
                        "name": self.settings.agent_name,
                        "description": self.settings.agent_description,
                        "tools": _safe_json(self.settings.agent_tools, []),
                        "environment": _safe_json(
                            self.settings.agent_environment, {}),
                        "capabilities": ["chat", "cancel", "streaming",
                                         "activity", "owns_sessions"]}}))
        # projection mode: replay/recover once, then publish a snapshot on
        # every (re)connect so the hub reconciles
        await self.agent.on_connect()
        hb = asyncio.create_task(self._heartbeat(ws))
        # run-facing file contract, only when delegation is switched on
        dropbox_task = None
        if self.settings.delegation_enabled:
            dropbox_task = asyncio.create_task(
                DropBox(self.settings.delegation_dir,
                        DelegationClient(self.settings, self)).run())
        sync_task = asyncio.create_task(self.agent.sync_loop())
        try:
            while True:
                env = Envelope.model_validate_json(await ws.recv())
                if self.resolve(env):
                    continue
                await self.agent.handle(env)
        except (ConnectionError, websockets.ConnectionClosed):
            pass
        finally:
            hb.cancel()
            sync_task.cancel()
            if dropbox_task is not None:
                dropbox_task.cancel()
            # locally-owned runs keep going across hub disconnects; the
            # reconnect snapshot reconciles the hub projection
            self.ws = None
            # in-flight call()s fail now, not at their timeout (mirrors
            # manager AgentConnection.close)
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("agent disconnected"))
            self._pending.clear()

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_interval)
            await ws.send(json.dumps({"protocol_version": PROTO,
                                      "type": AGENT_HEARTBEAT, "payload": {}}))

    async def run_forever(self) -> None:
        attempt = 0
        while True:
            try:
                async with websockets.connect(
                    self.settings.manager_url,
                    additional_headers={"authorization":
                                        f"Bearer {self.settings.agent_token}"},
                    max_size=16 * 1024 * 1024,
                ) as ws:
                    attempt = 0
                    await self.session(ws)
            except Exception:
                logger.exception("adapter session ended; reconnecting")
            delay = next_backoff(attempt, self.settings.reconnect_max)
            attempt += 1
            logger.info("reconnecting to %s in %.0fs (attempt %d)",
                        self.settings.manager_url, delay, attempt)
            await asyncio.sleep(delay)
