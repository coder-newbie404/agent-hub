"""Runtime contract suite: every built-in runtime, driven through the real
AdapterAgent -> SessionRuntime -> JsonlProcessRunner stack against its
committed stub binary. Assertions are on what the hub would receive (envelopes)
and on what the journal records, never on mocks."""
import asyncio
import json
import os
import pathlib

import pytest

from agent_adapter.agent import AdapterAgent
from agent_adapter.client import AdapterClient
from agent_adapter.config import Settings
from agent_adapter.protocol import Envelope
from agent_adapter.runtimes import get_runtime

STUBS = pathlib.Path(__file__).parent / "stubs"
RUNTIMES = ["omp", "claude", "opencode"]
# argv flag that carries the resume id, per runtime
RESUME_FLAG = {"omp": "-r", "claude": "--resume", "opencode": "-s"}


@pytest.fixture(params=RUNTIMES)
def rt(request):
    return request.param


def stub_for(rt: str) -> str:
    return str(STUBS / f"stub_{rt}.sh")


def make_settings(tmp_path, rt: str, **kw):
    kw.setdefault("allow_payload_env", True)
    return Settings(manager_url="wss://x", agent_token="t",
                    default_workspace=str(tmp_path), runtime=rt,
                    runtime_bin=stub_for(rt),
                    journal_dir=str(tmp_path / "journal"), **kw)


class Recorder:
    """Recording `send`: collects every envelope the agent emits."""

    def __init__(self):
        self.envs: list[Envelope] = []

    async def __call__(self, env: Envelope):
        self.envs.append(env)

    def types(self):
        return [e.type for e in self.envs]

    def of(self, type_: str):
        return [e for e in self.envs if e.type == type_]


async def wait_until(predicate, timeout=15.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def settled(agent):
    """The run task's `finally` (journal run_finished, registry cleanup)
    is its last act; wait for it by state, not by a sleep."""
    await wait_until(lambda: not agent.runtime._active)


async def start_agent(tmp_path, rt, **kw):
    send = Recorder()
    agent = AdapterAgent(make_settings(tmp_path, rt, **kw), send)
    await agent.on_connect()
    await agent.handle(Envelope(type="session.create", request_id="rc",
                                session_id="sess_1",
                                payload={"workspace": str(tmp_path)}))
    return agent, send


async def say(agent, text="hi", request_id="r1", env=None):
    payload = {"text": text}
    if env is not None:
        payload["env"] = env
    await agent.handle(Envelope(type="session.message", request_id=request_id,
                                session_id="sess_1", payload=payload))


def journal_events(agent):
    return agent.runtime.journal.read_from(0, 10_000)[0]


def read_args(path: pathlib.Path) -> list[str]:
    return path.read_text().splitlines()


async def test_reply_then_completed_carries_native_session_id(tmp_path, rt):
    agent, send = await start_agent(tmp_path, rt)
    await say(agent)
    await settled(agent)
    types = send.types()
    assert types.index("agent.message") < types.index("session.completed")
    assert send.of("agent.message")[0].payload == {"text": "stub reply"}
    done = send.of("session.completed")[0]
    assert done.payload == {"omp_session_id": f"stub-{rt}-1"}
    assert done.request_id == "r1" and done.session_id == "sess_1"
    # journal: the native id and the runtime that owns it are recorded
    upserts = [e for e in journal_events(agent)
               if e["kind"] == "session_upserted"
               and e.get("omp") == f"stub-{rt}-1"]
    assert upserts and upserts[-1]["rt"] == rt and upserts[-1]["status"] == "idle"
    assert {"kind": "message", "role": "assistant",
            "content": "stub reply"}.items() <= next(
        e for e in journal_events(agent)
        if e["kind"] == "message" and e["role"] == "assistant").items()


async def test_second_message_resumes_the_native_session(tmp_path, rt):
    args = tmp_path / "args.txt"
    agent, send = await start_agent(tmp_path, rt)
    env = {"STUB_ARGS_FILE": str(args)}
    await say(agent, "one", "r1", env)
    await settled(agent)
    assert RESUME_FLAG[rt] not in read_args(args)  # nothing to resume yet
    await say(agent, "two", "r2", env)
    await settled(agent)
    argv = read_args(args)
    flag = RESUME_FLAG[rt]
    assert flag in argv
    assert argv[argv.index(flag) + 1] == f"stub-{rt}-1"
    assert len(send.of("session.completed")) == 2


async def test_stream_emits_tool_activity(tmp_path, rt):
    agent, send = await start_agent(tmp_path, rt)
    await say(agent, env={"STUB_STREAM": "1"})
    await settled(agent)
    tools = [e for e in send.of("session.activity")
             if e.payload.get("kind") == "tool"]
    assert {e.payload.get("state") for e in tools} == {"start", "end"}
    assert all(e.session_id == "sess_1" and e.request_id == "r1"
               for e in tools)
    assert send.of("agent.message")[0].payload == {"text": "stub stream"}


class _RegisterWS:
    """Minimal socket: records what the adapter sends, closes on first recv."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        raise ConnectionError("closed")


async def test_delta_contract_matches_advertised_streaming_capability(
        tmp_path, rt):
    """Streaming runtimes advertise `streaming` and deliver token deltas. A
    non-streaming runtime does not advertise it, and whatever preview it
    sends is the complete final reply, never a partial fragment."""
    settings = make_settings(tmp_path, rt, delegation_enabled=False)
    send = Recorder()
    agent = AdapterAgent(settings, send)
    ws = _RegisterWS()
    await AdapterClient(settings, agent).session(ws)
    caps = ws.sent[0]["payload"]["capabilities"]
    streaming = "streaming" in caps
    assert streaming is get_runtime(rt).streaming

    await agent.handle(Envelope(type="session.create", request_id="rc",
                                session_id="sess_1",
                                payload={"workspace": str(tmp_path)}))
    await say(agent, env={"STUB_STREAM": "1"})
    await settled(agent)
    final = send.of("agent.message")[0].payload["text"]
    assert final == "stub stream"
    texts = [e.payload["text"] for e in send.of("session.delta")
             if "text" in e.payload]
    if streaming:
        assert len(texts) > 1  # incremental fragments
        assert "".join(texts) == final
    else:
        assert all(t == final for t in texts)  # whole-part preview only


async def test_failing_binary_reports_session_failed(tmp_path, rt):
    agent, send = await start_agent(tmp_path, rt)
    await say(agent, env={"STUB_FAIL": "1"})
    await settled(agent)
    assert "session.completed" not in send.types()
    failed = send.of("session.failed")
    assert len(failed) == 1
    assert failed[0].request_id == "r1" and failed[0].session_id == "sess_1"
    assert failed[0].payload["reason"].startswith(f"{rt} exited 1")
    assert any(e["kind"] == "run_finished" and e["status"] == "failed"
               for e in journal_events(agent))


async def test_failing_binary_reason_carries_stderr(tmp_path, rt):
    agent, send = await start_agent(tmp_path, rt)
    await say(agent, env={"STUB_FAIL": "1"})
    await settled(agent)
    assert "stub exploded" in send.of("session.failed")[0].payload["reason"]


async def test_cancel_acks_fails_cancelled_and_kills_process_group(
        tmp_path, rt):
    agent, send = await start_agent(tmp_path, rt)
    await say(agent, env={"STUB_DELAY": "10"})
    await wait_until(lambda: getattr(
        agent.runtime._runners.get("sess_1"), "pid", None) is not None)
    pid = agent.runtime._runners["sess_1"].pid
    try:
        await agent.handle(Envelope(type="session.cancel", request_id="r2",
                                    session_id="sess_1", payload={}))
        await settled(agent)
    finally:
        await agent.stop_work()
    ack = send.of("session.cancel_ack")[0]
    assert ack.request_id == "r2" and ack.payload == {"state": "accepted"}
    failed = send.of("session.failed")
    assert [f.payload for f in failed] == [{"reason": "cancelled"}]
    assert "session.completed" not in send.types()
    with pytest.raises(ProcessLookupError):  # whole group, incl. `sleep`
        os.killpg(pid, 0)


async def test_second_message_while_running_is_busy(tmp_path, rt):
    agent, send = await start_agent(tmp_path, rt)
    try:
        await say(agent, "slow", "r1", {"STUB_DELAY": "10"})
        await say(agent, "again", "r2")
        busy = send.of("session.failed")
        assert len(busy) == 1
        assert busy[0].request_id == "r2"
        assert busy[0].payload == {"reason": "busy"}
        assert "session.completed" not in send.types()
    finally:
        await agent.stop_work()


async def test_child_env_is_allow_listed(tmp_path, rt, monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN", "super-secret-token-value")
    monkeypatch.setenv("MANAGER_URL", "wss://hub.invalid/ws")
    env_file = tmp_path / "env.txt"
    agent, send = await start_agent(tmp_path, rt)
    await say(agent, env={"STUB_ENV_FILE": str(env_file)})
    await settled(agent)
    assert send.of("session.completed")
    text = env_file.read_text()
    keys = {line.split("=", 1)[0] for line in text.splitlines() if "=" in line}
    assert "AGENT_TOKEN" not in keys and "MANAGER_URL" not in keys
    assert "super-secret-token-value" not in text
    assert "PATH" in keys and "ADAPTER_DELEGATION_DIR" in keys


async def test_payload_env_ignored_unless_allowed(tmp_path, rt):
    agent, send = await start_agent(tmp_path, rt, allow_payload_env=False)
    await say(agent, env={"STUB_FAIL": "1"})
    await settled(agent)
    assert send.of("session.completed")
    assert "session.failed" not in send.types()


@pytest.mark.parametrize("seeded_rt", [None, "other"])
async def test_journal_runtime_mismatch_starts_fresh(tmp_path, rt, seeded_rt):
    """A native id belongs to the runtime that issued it. An entry owned by
    another runtime — or a legacy entry without `rt`, which means omp — is
    not resumed; the next run re-stamps it with the current runtime."""
    if seeded_rt == "other":
        seeded_rt = "claude" if rt != "claude" else "omp"
    owner = seeded_rt or "omp"
    resumable = owner == rt  # legacy entry + omp runtime is a legitimate resume
    event = {"kind": "session_upserted", "local_id": "sess_1",
             "omp": "foreign-native-id", "ws": str(tmp_path),
             "status": "idle", "updated_at": "2026-01-01T00:00:00+00:00"}
    if seeded_rt:
        event["rt"] = seeded_rt
    journal = tmp_path / "journal"
    journal.mkdir()
    (journal / "journal.jsonl").write_text(json.dumps(event) + "\n")
    args = tmp_path / "args.txt"
    agent, send = await start_agent(tmp_path, rt)
    await say(agent, env={"STUB_ARGS_FILE": str(args)})
    await settled(agent)
    argv = read_args(args)
    assert ("foreign-native-id" in argv) is resumable
    if not resumable:
        assert RESUME_FLAG[rt] not in argv
    last = [e for e in journal_events(agent)
            if e["kind"] == "session_upserted"][-1]
    assert last["rt"] == rt and last["omp"] == f"stub-{rt}-1"


def test_opencode_refuses_readonly_policy(tmp_path):
    with pytest.raises(ValueError, match="opencode.*policies"):
        make_settings(tmp_path, "opencode", policy="readonly")
    make_settings(tmp_path, "opencode", policy="full")  # supported


def test_unknown_runtime_error_lists_available_names(tmp_path):
    with pytest.raises(ValueError) as exc:
        make_settings(tmp_path, "nope")
    for name in RUNTIMES:
        assert name in str(exc.value)
    assert "nope" in str(exc.value)
