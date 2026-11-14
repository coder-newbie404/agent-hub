"""Bridge tests: hub envelopes → SessionRuntime calls → envelopes out."""
import asyncio
import json
import os
import pathlib

import pytest

import agent_adapter.runtime as runtime_mod
from agent_adapter.agent import AdapterAgent
from agent_adapter.config import Settings
from agent_adapter.protocol import Envelope
from agent_adapter.runtimes.base import Activity, Delta, RunResult

STUB = str(pathlib.Path(__file__).parent / "stubs" / "stub_omp.sh")


class FakeSpec:
    """RuntimeSpec stand-in whose runner is built by `runner_cls`."""
    name = "omp"
    streaming = True
    policies = frozenset({"default"})
    titles = None

    def __init__(self, runner_cls):
        self.make_runner = lambda settings: runner_cls()


def use_runner(monkeypatch, runner_cls):
    monkeypatch.setattr(runtime_mod, "get_runtime",
                        lambda name: FakeSpec(runner_cls))


async def wait_until(predicate, timeout=10.0):
    """Poll until `predicate()` is true; fail loudly instead of hanging."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


class RecordingRunner:
    """Runner stand-in recording (workspace, resume_id) per run."""
    calls: list = []
    pid = None
    last_stderr_tail = ""

    async def run(self, req, emit, on_spawn=None):
        RecordingRunner.calls.append((req.workspace, req.resume_id))
        return RunResult("stub-session-2", "ok")

    async def cancel(self):
        pass


def make_settings(tmp_path, **kw):
    return Settings(manager_url="wss://x", agent_token="t",
                    default_workspace=str(tmp_path), runtime="omp",
                    runtime_bin=STUB, allow_payload_env=True,
                    state_file=str(tmp_path / "state.json"),
                    journal_dir=str(tmp_path / "journal"), **kw)


class Recorder:
    def __init__(self):
        self.envs: list[Envelope] = []

    async def __call__(self, env: Envelope):
        self.envs.append(env)

    def types(self):
        return [e.type for e in self.envs]


async def test_create_validates_workspace(tmp_path):
    send = Recorder()
    agent = AdapterAgent(make_settings(tmp_path), send)
    await agent.handle(Envelope(type="session.create", request_id="req_1",
                                session_id="sess_1", payload={"workspace": "/nope/nothing"}))
    assert send.types() == ["session.failed"]
    await agent.handle(Envelope(type="session.create", request_id="req_2",
                                session_id="sess_1", payload={"workspace": str(tmp_path)}))
    assert send.envs[-1].type == "session.waiting"
    assert send.envs[-1].payload == {"state": "created"}
    assert send.envs[-1].request_id == "req_2"


async def test_message_run_completes_and_persists_state(tmp_path):
    send = Recorder()
    agent = AdapterAgent(make_settings(tmp_path), send)
    await agent.handle(Envelope(type="session.create", request_id="r1",
                                session_id="sess_1",
                                payload={"workspace": str(tmp_path)}))
    await agent.handle(Envelope(type="session.message", request_id="r2",
                                session_id="sess_1", payload={"text": "do it"}))
    # handle() returns before the spawned run task executes; wait for it to finish.
    await asyncio.wait_for(agent.runtime._active["sess_1"], 10)
    types = send.types()
    assert types[send.types().index("agent.message") - 1] == "session.waiting"
    assert "agent.message" in types and "session.completed" in types
    msg = send.envs[send.types().index("agent.message")]
    assert msg.payload == {"text": "stub reply"}
    done = send.envs[send.types().index("session.completed")]
    assert done.payload == {"omp_session_id": "stub-omp-1"}
    assert agent.runtime._state["sess_1"] == {"omp": "stub-omp-1",
                                              "rt": "omp",
                                              "ws": str(tmp_path),
                                              "status": "idle"}


async def test_second_message_while_running_is_busy(tmp_path):
    send = Recorder()
    agent = AdapterAgent(make_settings(tmp_path), send)
    await agent.handle(Envelope(type="session.create", request_id="r1",
                                session_id="sess_1",
                                payload={"workspace": str(tmp_path)}))
    await agent.handle(Envelope(type="session.message", request_id="r2",
                                session_id="sess_1",
                                payload={"text": "slow", "env": {"STUB_DELAY": "10"}}))
    await agent.handle(Envelope(type="session.message", request_id="r3",
                                session_id="sess_1", payload={"text": "again"}))
    assert send.envs[-1].type == "session.failed"
    assert send.envs[-1].payload == {"reason": "busy"}
    await agent.stop_work()


async def test_cancel_kills_run_and_acks(tmp_path):
    send = Recorder()
    agent = AdapterAgent(make_settings(tmp_path), send)
    await agent.handle(Envelope(type="session.create", request_id="r1",
                                session_id="sess_1",
                                payload={"workspace": str(tmp_path)}))
    await agent.handle(Envelope(type="session.message", request_id="r2",
                                session_id="sess_1",
                                payload={"text": "slow", "env": {"STUB_DELAY": "10"}}))
    # Cancel only makes sense once the child exists: wait (bounded, so a
    # broken run fails fast) for the runner to have spawned, then cancel.
    # `running` is sent before the spawn, and the runner records its pid only
    # after the spawn completed.
    await wait_until(lambda: getattr(
        agent.runtime._runners.get("sess_1"), "pid", None) is not None)
    pid = agent.runtime._runners["sess_1"].pid
    await agent.handle(Envelope(type="session.cancel", request_id="r3",
                                session_id="sess_1", payload={}))
    # session.failed is the run task's last act: wait for it by event, not by
    # a fixed sleep (a loaded machine stretches fork/kill latency)
    await wait_until(lambda: "session.failed" in send.types())
    await wait_until(lambda: not agent.runtime._active)
    ack = send.envs[send.types().index("session.cancel_ack")]
    assert ack.request_id == "r3" and ack.session_id == "sess_1"
    assert ack.payload == {"state": "accepted"}
    failed = send.envs[send.types().index("session.failed")]
    assert failed.payload == {"reason": "cancelled"}
    with pytest.raises(ProcessLookupError):  # whole process group is gone
        os.killpg(pid, 0)


async def test_cancel_without_run_acks_already_finished(tmp_path):
    send = Recorder()
    agent = AdapterAgent(make_settings(tmp_path), send)
    await agent.handle(Envelope(type="session.cancel", request_id="r9",
                                session_id="sess_none", payload={}))
    assert send.types() == ["session.cancel_ack"]
    assert send.envs[-1].payload == {"state": "already_finished"}
    assert send.envs[-1].request_id == "r9"


async def test_cancel_before_run_task_starts_still_emits_cancelled(tmp_path):
    send = Recorder()
    agent = AdapterAgent(make_settings(tmp_path), send)
    await agent.handle(Envelope(type="session.create", request_id="r1",
                                session_id="sess_1",
                                payload={"workspace": str(tmp_path)}))
    await agent.handle(Envelope(type="session.message", request_id="r2",
                                session_id="sess_1",
                                payload={"text": "slow", "env": {"STUB_DELAY": "10"}}))
    await agent.handle(Envelope(type="session.cancel", request_id="r3",
                                session_id="sess_1", payload={}))
    task = agent.runtime._active.get("sess_1")
    if task is not None:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
    failed = [e for e in send.envs if e.type == "session.failed"]
    assert len(failed) == 1
    assert failed[0].payload == {"reason": "cancelled"}
    assert agent.runtime._active == {}


async def test_workspace_binding_survives_restart(tmp_path, monkeypatch):
    use_runner(monkeypatch, RecordingRunner)
    RecordingRunner.calls = []

    ws_a = tmp_path / "project-a"
    ws_a.mkdir()

    # first adapter process: create alone journals the workspace binding…
    agent = AdapterAgent(make_settings(tmp_path), Recorder())
    await agent.handle(Envelope(type="session.create", request_id="r1",
                                session_id="sess_1",
                                payload={"workspace": str(ws_a)}))
    assert agent.runtime._state["sess_1"] == {"omp": None, "ws": str(ws_a),
                                              "status": "idle"}

    # …and a run records the omp session id next to it
    await agent.handle(Envelope(type="session.message", request_id="r2",
                                session_id="sess_1", payload={"text": "go"}))
    await asyncio.wait_for(agent.runtime._active["sess_1"], 5)
    assert agent.runtime._state["sess_1"] == {
        "omp": "stub-session-2", "rt": "omp", "ws": str(ws_a),
        "status": "idle"}

    # second adapter process (restart): journal replay restores the binding,
    # so the resumed run must execute in project-a with the recorded omp
    # session id — not the default workspace
    resumed = AdapterAgent(make_settings(tmp_path), Recorder())
    await resumed.on_connect()
    assert resumed.runtime._state["sess_1"] == {
        "omp": "stub-session-2", "rt": "omp", "ws": str(ws_a),
        "status": "idle"}
    await resumed.handle(Envelope(type="session.message", request_id="r3",
                                  session_id="sess_1", payload={"text": "again"}))
    await asyncio.wait_for(resumed.runtime._active["sess_1"], 5)
    assert RecordingRunner.calls == [(str(ws_a), None),
                                     (str(ws_a), "stub-session-2")]


async def test_legacy_state_file_imported_on_first_start(tmp_path, monkeypatch):
    use_runner(monkeypatch, RecordingRunner)
    RecordingRunner.calls = []

    # v1 state file: plain omp id strings, no workspace recorded
    (tmp_path / "state.json").write_text(json.dumps({"sess_old": "omp-old"}))
    agent = AdapterAgent(make_settings(tmp_path), Recorder())
    await agent.on_connect()
    assert agent.runtime._state["sess_old"] == {"omp": "omp-old", "ws": None,
                                                "status": "idle"}

    # the legacy session still resumes with its omp id; workspace falls
    # back to the configured default
    await agent.handle(Envelope(type="session.message", request_id="r1",
                                session_id="sess_old", payload={"text": "hi"}))
    await asyncio.wait_for(agent.runtime._active["sess_old"], 5)
    assert RecordingRunner.calls == [(str(tmp_path), "omp-old")]


async def test_on_connect_publishes_snapshot(tmp_path):
    send = Recorder()
    agent = AdapterAgent(make_settings(tmp_path), send)
    await agent.on_connect()
    assert send.envs[-1].type == "sessions.snapshot"
    assert send.envs[-1].payload == {"sessions": [], "done": True}

    await agent.handle(Envelope(type="session.create", request_id="r1",
                                session_id="sess_1",
                                payload={"workspace": str(tmp_path)}))
    await agent.on_connect()  # every (re)connect re-publishes
    snap = send.envs[-1]
    assert snap.type == "sessions.snapshot"
    assert snap.payload == {"sessions": [{"local_id": "sess_1",
                                          "status": "idle",
                                          "workspace": str(tmp_path),
                                          "omp": None,
                                          "title": None}],
                            "done": True}


class StreamRunner:
    """Runner stand-in replaying canned runtime events."""
    pid = None
    last_stderr_tail = ""

    async def run(self, req, emit, on_spawn=None):
        for event in [Activity("think"),
                      Activity("tool", "start", "bash", "Running echo"),
                      Activity("tool", "end", "bash"),
                      Delta(reset=True),
                      Delta("hi")]:
            await emit(event)
        return RunResult("stub-session-2", "hi")

    async def cancel(self):
        pass


async def test_run_streams_delta_and_activity(tmp_path, monkeypatch):
    use_runner(monkeypatch, StreamRunner)
    send = Recorder()
    agent = AdapterAgent(make_settings(tmp_path), send)
    await agent.handle(Envelope(type="session.create", request_id="r1",
                                session_id="sess_1",
                                payload={"workspace": str(tmp_path)}))
    await agent.handle(Envelope(type="session.message", request_id="r2",
                                session_id="sess_1", payload={"text": "go"}))
    await asyncio.wait_for(agent.runtime._active["sess_1"], 5)

    activity = [e for e in send.envs if e.type == "session.activity"]
    assert [a.payload for a in activity] == [
        {"kind": "think"},
        {"kind": "tool", "state": "start", "tool": "bash",
         "detail": "Running echo"},
        {"kind": "tool", "state": "end", "tool": "bash"},
    ]
    deltas = [e for e in send.envs if e.type == "session.delta"]
    assert deltas[0].payload == {"reset": True}
    assert deltas[1].payload == {"text": "hi"}

    # the streamed UX never replaces the authoritative final reply
    assert send.envs[-2].type == "agent.message"
    assert send.envs[-1].type == "session.completed"


async def test_sync_loop_pushes_journal_tail_and_rewinds_on_ack(tmp_path,
                                                                monkeypatch):
    use_runner(monkeypatch, RecordingRunner)
    send = Recorder()
    settings = make_settings(tmp_path, sync_interval=3600)
    agent = AdapterAgent(settings, send)
    await agent.on_connect()
    agent._sync_cursor_ready = True  # acked at least once
    agent.runtime.upsert_session("s1", "/w")
    agent.runtime.journal_user_message("s1", "hello", "req_1")
    await agent.push_pending()
    push = send.envs[-1]
    assert push.type == "sync.push"
    assert push.payload["instance"] == agent.runtime.instance
    assert push.payload["cursor_start"] == 0
    assert push.payload["cursor_end"] == 3  # instance + upsert + message
    # hub says it is only at 1 (gap): adapter rewinds
    await agent.handle(Envelope(type="sync.ack", payload={
        "instance": agent.runtime.instance, "cursor": 1, "catalog": []}))
    assert agent._sync_cursor == 1
    await agent.push_pending()
    push = send.envs[-1]
    assert push.payload["cursor_start"] == 1


async def test_ack_catalog_gap_triggers_need(tmp_path, monkeypatch):
    use_runner(monkeypatch, RecordingRunner)
    send = Recorder()
    agent = AdapterAgent(make_settings(tmp_path, sync_interval=3600), send)
    await agent.on_connect()
    agent.runtime.upsert_session("omp-abc", "/w")
    agent._sync_cursor_ready = True
    await agent.handle(Envelope(type="sync.ack", payload={
        "instance": agent.runtime.instance, "cursor": 0,
        "catalog": [{"omp_session_id": "omp-abc",
                     "hub_session_id": "sess_z",
                     "message_count": 4, "last_message_id": 9}]}))
    need = send.envs[-1]
    assert need.type == "sync.need"
    assert need.payload == {"omp_session_id": "omp-abc",
                            "after_message_id": 0}


async def test_ack_catalog_skips_entries_without_omp_id(tmp_path, monkeypatch):
    """Hub-internal rows (NULL omp_session_id) must not become local
    sessions: adopting them by hub_session_id is how ghost ids leaked
    into the journal and rebound the hub row."""
    use_runner(monkeypatch, RecordingRunner)
    send = Recorder()
    agent = AdapterAgent(make_settings(tmp_path, sync_interval=3600), send)
    await agent.on_connect()
    agent._sync_cursor_ready = True
    await agent.handle(Envelope(type="sync.ack", payload={
        "instance": agent.runtime.instance, "cursor": 0,
        "catalog": [{"omp_session_id": None, "hub_session_id": "sess_hub1",
                     "message_count": 4, "last_message_id": 9}]}))
    assert "sess_hub1" not in agent.runtime._state
    assert not any(e.type == "sync.need" for e in send.envs)


async def test_sync_messages_merge_into_journal(tmp_path, monkeypatch):
    use_runner(monkeypatch, RecordingRunner)
    send = Recorder()
    agent = AdapterAgent(make_settings(tmp_path, sync_interval=3600), send)
    await agent.on_connect()
    agent.runtime.upsert_session("omp-abc", "/w")
    await agent.handle(Envelope(type="sync.messages", payload={
        "omp_session_id": "omp-abc", "hub_session_id": "sess_z",
        "messages": [{"id": 1, "role": "user", "content": "hi",
                      "request_id": None, "created_at": "t"}],
        "done": True}))
    assert agent.runtime.local_message_count("omp-abc") == 1
    assert agent.runtime.hub_last_msg("omp-abc") == 1


async def test_sync_now_nudge_runs_pending_push(tmp_path, monkeypatch):
    use_runner(monkeypatch, RecordingRunner)
    send = Recorder()
    agent = AdapterAgent(make_settings(tmp_path, sync_interval=3600), send)
    await agent.on_connect()
    agent._sync_cursor_ready = True
    agent.runtime.upsert_session("s1", "/w")
    await agent.handle(Envelope(type="sync.now", payload={}))
    await asyncio.wait_for(agent._idle.wait(), 5)  # push drained
    assert any(e.type == "sync.push" for e in send.envs)


async def test_ack_from_stale_instance_adopts_empty_hub(tmp_path, monkeypatch):
    use_runner(monkeypatch, RecordingRunner)
    send = Recorder()
    agent = AdapterAgent(make_settings(tmp_path, sync_interval=3600), send)
    await agent.on_connect()
    agent.runtime.upsert_session("s1", "/w")
    assert not agent._sync_cursor_ready
    # hub lost its DB (or first deploy): ack carries instance ""/other and
    # a meaningless cursor — treat the hub as empty and rewind to 0
    await agent.handle(Envelope(type="sync.ack", payload={
        "instance": "different", "cursor": 999, "catalog": []}))
    assert agent._sync_cursor_ready is True
    assert agent._sync_cursor == 0
    await agent.push_pending()
    push = send.envs[-1]
    assert push.type == "sync.push"
    assert push.payload["instance"] == agent.runtime.instance
    assert push.payload["cursor_start"] == 0
    assert push.payload["cursor_end"] == 2  # journal_started + upsert


async def test_ack_resolves_omp_id_to_local_session(tmp_path, monkeypatch):
    """The hub catalogs completed Discord sessions by omp id while the
    journal keys them by the hub-shared local id: the ack must resolve
    through the omp index — no ghost adoption, need keyed by local id."""
    use_runner(monkeypatch, RecordingRunner)
    send = Recorder()
    agent = AdapterAgent(make_settings(tmp_path, sync_interval=3600), send)
    await agent.on_connect()
    agent.runtime.upsert_session("sess_hub1", "/w")
    agent.runtime._state["sess_hub1"]["omp"] = "omp-777"
    agent._sync_cursor_ready = True
    await agent.handle(Envelope(type="sync.ack", payload={
        "instance": agent.runtime.instance, "cursor": 0,
        "catalog": [{"omp_session_id": "omp-777",
                     "hub_session_id": "sess_hub1",
                     "message_count": 4, "last_message_id": 9}]}))
    assert "omp-777" not in agent.runtime._state  # no ghost adoption
    need = send.envs[-1]
    assert need.type == "sync.need"
    assert need.payload == {"omp_session_id": "sess_hub1",
                            "after_message_id": 0}
