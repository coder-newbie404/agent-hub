"""HubClient: HTTP→exception translation, auth, and event reconnection."""
import json

import httpx
import pytest

from discord_frontend.errors import (AgentFailed, AgentOffline, HubError,
                                      NotFound, RunInProgress)
from discord_frontend.hub import HubClient


def make_hub(handler, **kw) -> HubClient:
    return HubClient("http://t", "tok", http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://t"), **kw)


async def test_auth_header_and_body_roundtrip():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "sess_1"})

    hub = make_hub(handler)
    row = await hub.open_session("a1", "o1", "/w")
    assert seen["auth"] == "Bearer tok"
    assert seen["body"] == {"agent_id": "a1", "owner_id": "o1",
                            "workspace": "/w", "title": None}
    assert row.id == "sess_1"


def _code_handler(status: int, code: str):
    def handler(request):
        return httpx.Response(status, json={"detail": {"code": code,
                                                       "message": "m"}})
    return handler


async def test_404_maps_to_not_found():
    hub = make_hub(_code_handler(404, "not_found"))
    with pytest.raises(NotFound):
        await hub.get_session("sess_x")


async def test_409_run_in_progress_maps():
    hub = make_hub(_code_handler(409, "run_in_progress"))
    with pytest.raises(RunInProgress):
        await hub.send_message("s", "hi")


async def test_409_agent_offline_maps():
    hub = make_hub(_code_handler(409, "agent_offline"))
    with pytest.raises(AgentOffline):
        await hub.send_message("s", "hi")


async def test_502_agent_failed_maps():
    hub = make_hub(_code_handler(502, "agent_failed"))
    with pytest.raises(AgentFailed):
        await hub.open_session("a", "o")


async def test_unknown_error_maps_to_hub_error():
    hub = make_hub(_code_handler(500, "kaboom"))
    with pytest.raises(HubError):
        await hub.list_sessions()


async def test_message_send_returns_request_id():
    hub = make_hub(lambda r: httpx.Response(200, json={"request_id": "req_9"}))
    assert await hub.send_message("s", "hi") == "req_9"


async def test_cancel_returns_bool():
    hub = make_hub(lambda r: httpx.Response(200, json={"cancelled": False}))
    assert await hub.cancel("s") is False


class FakeWSConn:
    """One scripted connection: yields frames then dies."""

    def __init__(self, frames):
        self._frames = list(frames)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._frames:
            return self._frames.pop(0)
        raise ConnectionError("closed")


async def test_events_reconnects_and_yields_frames():
    scripted = iter([[{"n": 1}], [{"n": 2}]])
    seen_headers = []

    def factory(url, headers):
        seen_headers.append((url, headers))
        return FakeWSConn(next(scripted))

    hub = make_hub(lambda r: httpx.Response(200, json={}),
                   ws_factory=factory, retry_delay=0)
    got = []
    async for frame in hub.events():
        got.append(frame)
        if len(got) == 2:
            break
    assert got == [{"n": 1}, {"n": 2}]
    assert seen_headers[0][0] == "ws://t/ws/ui"
    assert seen_headers[0][1]["authorization"] == "Bearer tok"
