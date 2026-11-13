"""Delegation client: the adapter acts on behalf of its agent's runs.

REST (directory, sessions, grants) + channel round-trips (ask). The
per-agent token never leaves this process; runs reach it through the
DropBox file contract."""
import asyncio
import json
import logging
import pathlib

import httpx

from omp_adapter.protocol import DELEGATE_MESSAGE, Envelope, new_id

logger = logging.getLogger(__name__)


def _base_url(manager_url: str) -> str:
    # wss://host/a/b/ws/agent → https://host/a/b
    rest = manager_url.split("://", 1)
    scheme = "https" if rest[0] == "wss" else "http"
    path = rest[1].rsplit("/ws/agent", 1)[0]
    return f"{scheme}://{path}"


class RestError(Exception):
    """Non-2xx from the hub REST API."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"http {status}")
        self.status = status
        self.detail = detail


class DelegationClient:
    def __init__(self, settings, channel):
        self.settings = settings
        self.channel = channel
        self._base = _base_url(settings.manager_url)

    def _headers(self) -> dict:
        return {"authorization": f"Bearer {self.settings.agent_token}"}

    async def _rest(self, method: str, path: str, body: dict | None = None):
        kwargs = {"json": body} if body is not None else {}
        async with httpx.AsyncClient(headers=self._headers(),
                                     timeout=30.0) as hc:
            resp = await getattr(hc, method.lower())(f"{self._base}{path}",
                                                     **kwargs)
            if not (200 <= resp.status_code < 300):
                raise RestError(resp.status_code, resp.text[:500])
            return resp.json()

    async def directory(self):
        return await self._rest("GET", "/agents")

    async def sessions_for(self, agent_id: str):
        # the manager route ignores query params; filter on our side
        rows = await self._rest("GET", "/sessions")
        return [r for r in rows if r.get("agent_id") == agent_id]

    async def create_session(self, agent_id: str, description: str):
        return await self._rest("POST", "/sessions",
                                {"agent_id": agent_id,
                                 "description": description})

    async def add_grant(self, session_id: str, agent_id: str, level: str):
        return await self._rest(
            "POST", f"/sessions/{session_id}/grants",
            {"agent_id": agent_id, "level": level})

    async def ask(self, session_id: str, text: str, hop: int = 0) -> dict:
        env = Envelope(type=DELEGATE_MESSAGE, request_id=new_id("req"),
                       session_id=session_id,
                       payload={"text": text, "hop": hop})
        result = await self.channel.call(env)
        return result.payload


class DropBox:
    """File contract for local runs: req-<id>.json in → .result.json out."""

    def __init__(self, directory: str, delegation: DelegationClient):
        self.dir = pathlib.Path(directory).expanduser()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.delegation = delegation

    async def run(self, interval: float = 0.5) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(interval)

    async def poll_once(self) -> int:
        handled = 0
        for req_path in sorted(self.dir.glob("req-*.json")):
            # results are req-<id>.result.json — same glob, never re-process
            if req_path.name.endswith(".result.json"):
                continue
            result_path = req_path.with_suffix(".result.json")
            if result_path.exists():
                continue
            try:
                request = json.loads(req_path.read_text())
                data = await self._dispatch(request)
                payload = {"ok": True, "data": data}
            except RestError as exc:  # HTTP failure is a first-class result
                payload = {"ok": False, "error": f"http {exc.status}",
                           "detail": exc.detail}
            except Exception as exc:  # a bad request must not kill the loop
                payload = {"ok": False, "error": str(exc)}
            result_path.write_text(json.dumps(payload))
            req_path.unlink(missing_ok=True)
            handled += 1
        return handled

    async def _dispatch(self, request: dict) -> dict:
        op = request.get("op")
        dc = self.delegation
        if op == "directory":
            return await dc.directory()
        if op == "sessions":
            return await dc.sessions_for(str(request["agent_id"]))
        if op == "create":
            return await dc.create_session(str(request["agent_id"]),
                                           str(request.get("description", "")))
        if op == "grant":
            return await dc.add_grant(str(request["session_id"]),
                                      str(request["agent_id"]),
                                      str(request["level"]))
        if op == "ask":
            return await dc.ask(str(request["session_id"]),
                                str(request["text"]),
                                int(request.get("hop", 0)))
        raise ValueError(f"unknown op: {op!r}")
