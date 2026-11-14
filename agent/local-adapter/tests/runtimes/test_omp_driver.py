import asyncio
import os
import pathlib
import sqlite3
from dataclasses import dataclass, field

import pytest

from agent_adapter.runtimes import get_runtime
from agent_adapter.runtimes.base import (Activity, Delta, RunError,
                                         RunRequest, RunResult)
from agent_adapter.runtimes.omp import POLICIES, omp_titles

STUB = str(pathlib.Path(__file__).parent.parent / "stubs" / "stub_omp.sh")
make_runner = get_runtime("omp").make_runner


@dataclass
class FakeSettings:
    runtime_bin: str = STUB
    stream_limit_bytes: int = 64 * 1024 * 1024
    env_passthrough: list = field(default_factory=list)
    delegation_enabled: bool = False
    delegation_dir: str = "/tmp/deleg"
    runtime_args: list = field(default_factory=list)
    omp_titles_db: str = ""
    model_fields = {}


async def _noop(event):
    pass


def runner(**kw):
    return make_runner(FakeSettings(**kw))


def req(text="hello", resume=None, **kw):
    return RunRequest("/tmp", resume, text, **kw)


async def test_run_parses_session_and_reply():
    r = runner()
    res = await r.run(req(), _noop)
    assert res == RunResult(resume_id="stub-omp-1", text="stub reply")


async def test_run_handles_oversized_event_line():
    r = runner()
    res = await asyncio.wait_for(
        r.run(req(env={"STUB_BIG_LINE": "1"}), _noop), 10)
    assert res.resume_id == "stub-omp-1"
    assert res.text == "BIG" * 40000


async def test_run_failure_raises_with_stderr():
    r = runner()
    with pytest.raises(RunError) as e:
        await r.run(req(env={"STUB_FAIL": "1"}), _noop)
    assert "stub exploded" in e.value.stderr_tail


async def test_cancel_kills_process_group():
    r = runner()
    started = asyncio.get_running_loop().time()
    task = asyncio.create_task(
        r.run(req("slow job", env={"STUB_DELAY": "10"}), _noop))
    await asyncio.sleep(0.3)  # stub is inside its sleep
    pid = r.pid
    assert pid is not None
    await r.cancel()
    with pytest.raises(RunError):
        await task
    assert asyncio.get_running_loop().time() - started < 5
    # the whole group (bash leader AND its `sleep` child) is gone
    for _ in range(50):
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("process group still alive after cancel")


async def test_run_survives_stderr_flood():
    r = runner()
    res = await asyncio.wait_for(
        r.run(req(env={"STUB_STDERR_FLOOD": "1"}), _noop), 10)
    assert res == RunResult("stub-omp-1", "stub reply")
    assert 0 < len(r.last_stderr_tail) <= 2000


async def test_mid_stream_failure_kills_child_and_raises_typed_error():
    # A line beyond the stream limit raised LimitOverrunError and orphaned the
    # child. Now: typed error, child reaped. readline's ValueError wording
    # varies with transport chunking, so pin the condition, not the message.
    r = runner(stream_limit_bytes=64 * 1024)
    with pytest.raises(RunError) as e:
        await asyncio.wait_for(
            r.run(req(env={"STUB_BIG_LINE": "1"}), _noop), 10)
    assert "limit" in str(e.value)
    assert r.proc is None


async def test_run_emits_normalized_events_in_order():
    events = []

    async def emit(ev):
        events.append(ev)

    r = runner()
    res = await r.run(req(env={"STUB_STREAM": "1"}), emit)
    assert res == RunResult("stub-omp-1", "stub stream")
    assert events == [
        Activity("think"),
        Activity("tool", "start", "bash", "Running echo"),
        Activity("tool", "end", "bash"),
        Delta(reset=True),
        Delta(text="stub "),
        Delta(text="stream"),
    ]


async def test_run_spawns_without_session_dir(tmp_path):
    # Regression: --session-dir sent every Discord-driven run to a flat store
    # the omp TUI /resume picker never reads. omp's default storage IS the
    # agent store, so the flag must stay out of the argv entirely.
    args_file = tmp_path / "argv"
    r = runner()
    await r.run(req(env={"STUB_ARGS_FILE": str(args_file)}), _noop)
    argv = args_file.read_text().splitlines()
    assert "--session-dir" not in argv
    assert "--mode=json" in argv and "--cwd" in argv
    await r.run(req(resume="omp-123", env={"STUB_ARGS_FILE": str(args_file)}),
                _noop)
    argv = args_file.read_text().splitlines()
    assert argv[argv.index("-r") + 1] == "omp-123"


async def test_model_and_runtime_args_reach_argv(tmp_path):
    args_file = tmp_path / "argv"
    r = runner(runtime_args=["--foo", "bar"])
    await r.run(req(model="m-1", env={"STUB_ARGS_FILE": str(args_file)}),
                _noop)
    argv = args_file.read_text().splitlines()
    assert argv[argv.index("--model") + 1] == "m-1"
    assert argv[argv.index("--foo") + 1] == "bar"
    assert argv[-3:] == ["-p", "--", "hello"]


async def test_child_env_is_allow_listed(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN", "secret")
    monkeypatch.setenv("MANAGER_URL", "wss://x")
    env_file = tmp_path / "env"
    r = runner()
    await r.run(req(env={"STUB_ENV_FILE": str(env_file)}), _noop)
    keys = {line.split("=", 1)[0] for line in env_file.read_text().splitlines()}
    assert "PATH" in keys
    assert not {"AGENT_TOKEN", "MANAGER_URL"} & keys
    assert "ADAPTER_DELEGATION_DIR" not in keys


async def test_delegation_dir_exported_when_enabled(tmp_path):
    env_file = tmp_path / "env"
    r = runner(delegation_enabled=True, delegation_dir="/tmp/deleg")
    await r.run(req(env={"STUB_ENV_FILE": str(env_file)}), _noop)
    lines = env_file.read_text().splitlines()
    assert "ADAPTER_DELEGATION_DIR=/tmp/deleg" in lines


def _titles_db(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE session_titles(session_id TEXT, title TEXT)")
    con.executemany("INSERT INTO session_titles VALUES (?, ?)", rows)
    con.commit()
    con.close()


def test_omp_titles_returns_only_nonempty_stripped(tmp_path):
    db = tmp_path / "t.db"
    _titles_db(db, [("a", "  Fix bug  "), ("b", "   "), ("c", None)])
    s = FakeSettings(omp_titles_db=str(db))
    assert omp_titles(s, ["a", "b", "c", "missing"]) == {"a": "Fix bug"}


def test_omp_titles_missing_file_and_no_ids(tmp_path):
    s = FakeSettings(omp_titles_db=str(tmp_path / "nope.db"))
    assert omp_titles(s, ["a"]) == {}
    db = tmp_path / "t.db"
    _titles_db(db, [("a", "T")])
    assert omp_titles(FakeSettings(omp_titles_db=str(db)), []) == {}


def test_omp_titles_corrupt_file_returns_empty(tmp_path):
    db = tmp_path / "bad.db"
    db.write_bytes(b"this is not a sqlite database" * 100)
    assert omp_titles(FakeSettings(omp_titles_db=str(db)), ["a"]) == {}


@pytest.mark.parametrize("policy,flag", [
    ("readonly", "--approval-mode=always-ask"),
    ("edits", "--approval-mode=write"),
    ("full", "--approval-mode=yolo"),
])
async def test_policy_maps_to_verified_approval_flag(tmp_path, policy, flag):
    args_file = tmp_path / "argv"
    r = runner()
    await r.run(req(policy=policy, env={"STUB_ARGS_FILE": str(args_file)}),
                _noop)
    assert flag in args_file.read_text().splitlines()


async def test_default_policy_adds_no_approval_flag(tmp_path):
    args_file = tmp_path / "argv"
    r = runner()
    await r.run(req(env={"STUB_ARGS_FILE": str(args_file)}), _noop)
    assert not any(a.startswith("--approval-mode")
                   for a in args_file.read_text().splitlines())


def test_spec_policies_match_table():
    assert get_runtime("omp").policies == frozenset(POLICIES)
    assert set(POLICIES) == {"default", "readonly", "edits", "full"}


@pytest.mark.parametrize("text", [
    "--version", "--approval-mode=yolo hello", "- hello", "-r other-session",
])
@pytest.mark.parametrize("policy", ["default", "readonly", "edits"])
async def test_flag_like_text_only_appears_after_option_terminator(
        tmp_path, text, policy):
    args_file = tmp_path / "argv"
    r = runner(runtime_args=["--foo"])
    await r.run(req(text, resume="sid-1", model="m-1", policy=policy,
                    env={"STUB_ARGS_FILE": str(args_file)}), _noop)
    argv = args_file.read_text().splitlines()
    assert argv[-2:] == ["--", text]
    assert argv[-3] == "-p"
    assert argv.count(text) == 1
    # only the policy contributes approval flags, all before the terminator
    options = argv[:argv.index("--")]
    approvals = [a for a in options if a.startswith("--approval-mode")]
    assert len(approvals) == (0 if policy == "default" else 1)
