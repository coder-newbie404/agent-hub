"""Run-facing CLI over the drop box: submit a request, wait for the result."""
import asyncio
import json
import threading
import time

import pytest

from agent_adapter.delegation import DropBox
from agent_adapter import cli


def _simulate_daemon(tmp_path, respond):
    """Answer the first request file like the daemon's DropBox watcher.

    Plain thread on purpose; daemon=True so a failure here can never hang
    the test process.
    """

    def daemon():
        for _ in range(100):
            reqs = sorted(tmp_path.glob("req-*.json"))
            if reqs:
                req = reqs[0]
                try:
                    request = json.loads(req.read_text())
                except json.JSONDecodeError:
                    pass  # CLI still writing the request; retry
                else:
                    (tmp_path / (req.stem + ".result.json")).write_text(
                        json.dumps(respond(request)))
                    req.unlink()
                    return
            time.sleep(0.02)

    thread = threading.Thread(target=daemon, daemon=True)
    thread.start()


def test_submit_and_collect(tmp_path, monkeypatch):
    monkeypatch.setenv("DELEGATION_DIR", str(tmp_path))
    _simulate_daemon(
        tmp_path, lambda request: {"ok": True, "data": {"echo": request}})
    out = cli.main(["ask", "--session", "s1", "--text", "hi",
                    "--timeout", "10"])
    assert out["data"]["echo"]["session_id"] == "s1"


def test_timeout_when_daemon_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("DELEGATION_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as ei:
        cli.main(["ask", "--session", "s1", "--text", "hi",
                  "--timeout", "0.1"])
    assert ei.value.code == 1


def test_error_result_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("DELEGATION_DIR", str(tmp_path))
    _simulate_daemon(tmp_path, lambda request: {"ok": False, "error": "busy"})
    with pytest.raises(SystemExit) as ei:
        cli.main(["ask", "--session", "s1", "--text", "hi",
                  "--timeout", "10"])
    assert ei.value.code == 2


class FakeDelegation:
    """Stands in for DelegationClient: records which ops reach it."""

    def __init__(self):
        self.calls = []

    async def directory(self):
        self.calls.append(("directory",))
        return [{"id": "a1"}]

    async def sessions_for(self, agent_id):
        self.calls.append(("sessions", agent_id))
        return [{"agent_id": agent_id}]

    async def create_session(self, agent_id, description):
        self.calls.append(("create", agent_id, description))
        return {"agent_id": agent_id, "description": description}

    async def add_grant(self, session_id, agent_id, level):
        self.calls.append(("grant", session_id, agent_id, level))
        return {"granted": level}

    async def ask(self, session_id, text, hop=0):
        self.calls.append(("ask", session_id, text, hop))
        return {"reply": text}


def _real_daemon(box):
    """Answer request files through the real DropBox.poll_once."""

    def daemon():
        for _ in range(100):
            if asyncio.run(box.poll_once()):
                return
            time.sleep(0.02)

    thread = threading.Thread(target=daemon, daemon=True)
    thread.start()


@pytest.mark.parametrize(
    "argv, expected_call, expected_data",
    [
        (["agents"], ("directory",), [{"id": "a1"}]),
        (["sessions", "--agent", "a1"], ("sessions", "a1"),
         [{"agent_id": "a1"}]),
        (["create", "--agent", "a1", "--description", "d"],
         ("create", "a1", "d"), {"agent_id": "a1", "description": "d"}),
        (["grant", "--session", "s1", "--agent", "a1", "--level", "write"],
         ("grant", "s1", "a1", "write"), {"granted": "write"}),
        (["ask", "--session", "s1", "--text", "hi", "--hop", "2"],
         ("ask", "s1", "hi", 2), {"reply": "hi"}),
    ])
def test_every_op_reaches_real_dispatcher(tmp_path, monkeypatch, argv,
                                          expected_call, expected_data):
    """CLI command names must speak the ops DropBox._dispatch knows."""
    monkeypatch.setenv("DELEGATION_DIR", str(tmp_path))
    fake = FakeDelegation()
    _real_daemon(DropBox(str(tmp_path), fake))
    out = cli.main(argv + ["--timeout", "10"])
    assert fake.calls == [expected_call]
    assert out["data"] == expected_data
