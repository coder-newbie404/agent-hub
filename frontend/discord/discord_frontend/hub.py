"""HTTP + event client: the frontend's only view of the hub.

Every call the in-process bot made against hub singletons crosses this
seam now — REST via httpx, session events via the /ws/ui websocket with
exponential-backoff reconnect."""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import httpx

from discord_frontend.errors import (AgentFailed, AgentOffline, HubError,
                                      NotFound, RunInProgress)

_BY_CODE = {
    "not_found": NotFound,
    "run_in_progress": RunInProgress,
    "agent_offline": AgentOffline,
    "agent_failed": AgentFailed,
}

_DT_FIELDS = {"created_at", "updated_at", "expires_at", "revoked_at",
              "posted_at", "last_seen"}


def _decode(x):
    """JSON rows → attribute rows (bot and ui code is written against
    attribute access); ISO timestamps → naive datetimes so existing
    format specs keep working."""
    if isinstance(x, list):
        return [_decode(i) for i in x]
    if not isinstance(x, dict):
        return x
    out = {}
    for k, v in x.items():
        if k in _DT_FIELDS and isinstance(v, str):
            try:
                v = datetime.fromisoformat(v)
            except ValueError:
                pass
        out[k] = v
    return SimpleNamespace(**out)


class HubClient:
    def __init__(self, base_url: str, token: str, http_client=None,
                 ws_factory=None, retry_delay: float = 1.0,
                 retry_cap: float = 60.0):
        self._http = http_client or httpx.AsyncClient(base_url=base_url)
        self._headers = {"authorization": f"Bearer {token}"}
        self._base_url = base_url.rstrip("/")
        self._ws_factory = ws_factory
        self._retry_delay = retry_delay
        self._retry_cap = retry_cap

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        r = await self._http.request(method, path, headers=self._headers,
                                     **kw)
        if r.status_code < 400:
            return r
        detail = {}
        try:
            detail = r.json().get("detail") or {}
        except Exception:
            pass
        code = detail.get("code") if isinstance(detail, dict) else None
        raise _BY_CODE.get(code, HubError)(
            detail.get("message", f"{method} {path} -> {r.status_code}"))

    async def _json(self, method: str, path: str, **kw):
        return (await self._request(method, path, **kw)).json()

    # --- sessions --------------------------------------------------------

    async def get_session(self, session_id: str) -> dict:
        return _decode(await self._json("GET", f"/sessions/{session_id}"))

    async def session_by_thread(self, thread_id: int) -> dict:
        return _decode(await self._json("GET", "/sessions",
                                params={"thread_id": thread_id}))

    async def list_sessions(self) -> list[dict]:
        return _decode(await self._json("GET", "/sessions"))

    async def messages(self, session_id: str, unposted: bool = False):
        params = {"unposted": "true"} if unposted else None
        return _decode(await self._json("GET", f"/sessions/{session_id}/messages",
                                params=params))

    async def mark_posted(self, session_id: str, ids: list[int]) -> int:
        body = await self._json(
            "POST", f"/sessions/{session_id}/messages/mark-posted",
            json={"ids": ids})
        return body["marked"]

    async def message_count(self, session_id: str) -> int:
        return (await self._json(
            "GET", f"/sessions/{session_id}/messages/count"))["count"]

    async def missed_session_ids(self) -> list[str]:
        return (await self._json("GET", "/sessions/missed"))["session_ids"]

    async def recover_interrupted(self) -> list[str]:
        return (await self._json(
            "POST", "/sessions/recover-interrupted"))["session_ids"]

    async def run_status(self, session_id: str) -> dict:
        return _decode(await self._json("GET", f"/sessions/{session_id}/run"))

    async def open_session(self, agent_id: str, owner_id: str,
                           workspace: str | None = None,
                           title: str | None = None) -> dict:
        return _decode(await self._json("POST", "/sessions", json={
            "agent_id": agent_id, "owner_id": owner_id,
            "workspace": workspace, "title": title}))

    async def send_message(self, session_id: str, text: str) -> str:
        body = await self._json("POST", f"/sessions/{session_id}/messages",
                                json={"text": text})
        return body["request_id"]

    async def cancel(self, session_id: str) -> bool:
        return (await self._json(
            "POST", f"/sessions/{session_id}/cancel"))["cancelled"]

    async def rename(self, session_id: str, title: str) -> dict:
        return _decode(await self._json("PATCH", f"/sessions/{session_id}",
                                json={"title": title}))

    async def set_archived(self, session_id: str, archived: bool) -> dict:
        return _decode(await self._json("PATCH", f"/sessions/{session_id}",
                                json={"archived": archived}))

    async def attach_thread(self, session_id: str, thread_id: int) -> dict:
        return _decode(await self._json("PATCH", f"/sessions/{session_id}",
                                json={"thread_id": thread_id}))

    async def delete_session(self, session_id: str) -> None:
        await self._request("DELETE", f"/sessions/{session_id}")

    # --- agents / sync / tokens -------------------------------------------

    async def agents(self) -> list[dict]:
        return _decode(await self._json("GET", "/agents"))

    async def sync_nudge(self) -> int:
        return (await self._json("POST", "/sync/nudge"))["nudged"]

    async def mint_token(self, agent_id: str, ttl_days: int | None = None):
        body = await self._json("POST", "/tokens",
                                json={"agent_id": agent_id,
                                      "ttl_days": ttl_days})
        return self._token_row(body), body["token"]

    async def rotate_token(self, agent_id: str):
        body = await self._json("POST", f"/tokens/{agent_id}/rotate")
        return self._token_row(body), body["token"]

    @staticmethod
    def _token_row(body: dict):
        return _decode({k: v for k, v in body.items() if k != "token"})

    async def revoke_token(self, agent_id: str) -> int:
        return (await self._json(
            "POST", f"/tokens/{agent_id}/revoke"))["revoked"]

    async def list_tokens(self) -> list[dict]:
        return _decode(await self._json("GET", "/tokens"))

    # --- events ------------------------------------------------------------

    def _ws_url(self) -> str:
        base = self._base_url.replace("https://", "wss://", 1)
        base = base.replace("http://", "ws://", 1)
        return f"{base}/ws/ui"

    async def events(self):
        """Yield hub event frames forever; reconnect with backoff."""
        delay = self._retry_delay
        url = self._ws_url()
        while True:
            try:
                if self._ws_factory is not None:
                    conn = self._ws_factory(url, dict(self._headers))
                else:
                    import websockets
                    conn = websockets.connect(url, additional_headers=dict(
                        self._headers))
                async with conn as ws:
                    delay = self._retry_delay
                    async for raw in ws:
                        if isinstance(raw, (str, bytes)):
                            raw = json.loads(raw)
                        yield raw
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._retry_cap)
