import asyncio
import json

import pytest

from omp_adapter.delegation import DelegationClient, DropBox


class FakeSettings:
    manager_url = "wss://hub.example/ws/agent"
    agent_token = "tok"


class FakeChannel:
    """Stands in for OMPClient: records calls, answers once."""

    def __init__(self, reply=None):
        self.sent = []
        self.reply = reply or {}

    async def call(self, env, timeout=600.0):
        self.sent.append(env)
        return type("E", (), {"payload": self.reply})()


@pytest.mark.asyncio
async def test_ask_sends_and_returns_payload():
    ch = FakeChannel(reply={"status": "completed", "reply": "3 ducks"})
    dc = DelegationClient(FakeSettings(), ch)
    out = await dc.ask("sess_1", "count ducks", hop=1)
    assert out["reply"] == "3 ducks"
    assert ch.sent[0].type == "delegate.message"
    assert ch.sent[0].payload["hop"] == 1
    assert ch.sent[0].request_id  # minted


@pytest.mark.asyncio
async def test_rest_base_url_and_directory(monkeypatch):
    dc = DelegationClient(FakeSettings(), FakeChannel())
    captured = {}

    class FakeResp:
        status_code = 200
        def json(self):
            return [{"id": "agent-b"}]

    class FakeAsyncClient:
        def __init__(self, headers=None, timeout=None):
            captured["headers"] = headers
            captured["timeout"] = timeout
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url):
            captured["url"] = url
            return FakeResp()

    monkeypatch.setattr("omp_adapter.delegation.httpx.AsyncClient",
                        FakeAsyncClient)
    out = await dc.directory()
    assert out == [{"id": "agent-b"}]
    assert captured["url"] == "https://hub.example/agents"
    assert captured["headers"]["authorization"] == "Bearer tok"
    assert captured["timeout"] == 30.0


@pytest.mark.asyncio
async def test_dropbox_ask_roundtrip(tmp_path):
    ch = FakeChannel(reply={"status": "completed", "reply": "done"})
    dc = DelegationClient(FakeSettings(), ch)
    box = DropBox(str(tmp_path), dc)
    req = tmp_path / "req-abc.json"
    req.write_text(json.dumps({"op": "ask", "session_id": "s1",
                               "text": "hello"}))
    assert await box.poll_once() == 1
    assert not req.exists()
    result = json.loads((tmp_path / "req-abc.result.json").read_text())
    assert result["ok"] is True
    assert result["data"]["reply"] == "done"


@pytest.mark.asyncio
async def test_dropbox_bad_op_reports_error(tmp_path):
    box = DropBox(str(tmp_path), DelegationClient(FakeSettings(),
                                                  FakeChannel()))
    req = tmp_path / "req-x.json"
    req.write_text(json.dumps({"op": "nope"}))
    assert await box.poll_once() == 1
    result = json.loads((tmp_path / "req-x.result.json").read_text())
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_sessions_for_filters_client_side(monkeypatch):
    dc = DelegationClient(FakeSettings(), FakeChannel())
    captured = {}

    class FakeResp:
        status_code = 200
        def json(self):
            return [{"id": "s1", "agent_id": "agent-a"},
                    {"id": "s2", "agent_id": "agent-b"},
                    {"id": "s3", "agent_id": "agent-a"}]

    class FakeAsyncClient:
        def __init__(self, headers=None, timeout=None):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url):
            captured["url"] = url
            return FakeResp()

    monkeypatch.setattr("omp_adapter.delegation.httpx.AsyncClient",
                        FakeAsyncClient)
    out = await dc.sessions_for("agent-a")
    # the manager route ignores query params: no ?agent= on the wire
    assert captured["url"] == "https://hub.example/sessions"
    assert out == [{"id": "s1", "agent_id": "agent-a"},
                   {"id": "s3", "agent_id": "agent-a"}]


@pytest.mark.asyncio
async def test_rest_non_2xx_reports_error_result(tmp_path, monkeypatch):
    dc = DelegationClient(FakeSettings(), FakeChannel())
    # 4xx/5xx and 3xx alike (httpx does not follow redirects by default)
    cases = {"status": 500, "text": "boom"}

    class FakeAsyncClient:
        def __init__(self, headers=None, timeout=None):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url):
            return type("FakeResp", (), {"status_code": cases["status"],
                                         "text": cases["text"]})()

    monkeypatch.setattr("omp_adapter.delegation.httpx.AsyncClient",
                        FakeAsyncClient)
    box = DropBox(str(tmp_path), dc)
    for name, code, text in [("req-d", 500, "boom"), ("req-r", 302, "")]:
        cases["status"], cases["text"] = code, text
        req = tmp_path / f"{name}.json"
        req.write_text(json.dumps({"op": "directory"}))
        assert await box.poll_once() == 1
        result = json.loads((tmp_path / f"{name}.result.json").read_text())
        assert result["ok"] is False
        assert result["error"] == f"http {code}"
        assert result["detail"] == text
