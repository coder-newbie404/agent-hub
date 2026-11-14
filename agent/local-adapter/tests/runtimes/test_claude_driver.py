import asyncio
import os
import pathlib
import signal
import time
from dataclasses import dataclass, field

import pytest

from agent_adapter.runtimes import get_runtime
from agent_adapter.runtimes.base import (Activity, Delta, RunError, RunRequest,
                                         RunResult)
from agent_adapter.runtimes.claude import (POLICIES, SPEC, ClaudeDriver,
                                           ClaudeParser)

STUB = str(pathlib.Path(__file__).parent.parent / "stubs" / "stub_claude.sh")
make_runner = get_runtime("claude").make_runner


@dataclass
class FakeSettings:
    runtime_bin: str = STUB
    stream_limit_bytes: int = 64 * 1024 * 1024
    env_passthrough: list = field(default_factory=list)
    delegation_enabled: bool = False
    delegation_dir: str = "/tmp/deleg"
    runtime_args: list = field(default_factory=list)
    model_fields = {}


async def _noop(event):
    pass


def runner(**kw):
    return make_runner(FakeSettings(**kw))


def req(text="hello", resume=None, **kw):
    return RunRequest("/tmp", resume, text, **kw)


async def _argv(tmp_path, *, resume=None, settings=None, **kw):
    args_file = tmp_path / "argv"
    r = make_runner(settings or FakeSettings())
    await r.run(req(resume=resume,
                    env={"STUB_ARGS_FILE": str(args_file)}, **kw), _noop)
    return args_file.read_text().splitlines()


# ---- run / events --------------------------------------------------------

async def test_run_parses_session_and_reply():
    res = await runner().run(req(), _noop)
    assert res == RunResult(resume_id="stub-claude-1", text="stub reply")


async def test_run_emits_text_deltas_after_reset():
    events = []

    async def emit(ev):
        events.append(ev)

    await runner().run(req(), emit)
    assert events == [Delta(reset=True), Delta("stub "), Delta("reply")]


async def test_run_emits_tool_activity_with_name_resolved_from_use_id():
    events = []

    async def emit(ev):
        events.append(ev)

    res = await runner().run(req(env={"STUB_STREAM": "1"}), emit)
    assert res == RunResult("stub-claude-1", "stub stream")
    assert events == [
        Activity("tool", "start", "Bash", "echo hi"),
        Activity("tool", "end", "Bash"),
        Delta(reset=True),
        Delta("stub "),
        Delta("stream"),
    ]


async def test_run_handles_oversized_event_line():
    res = await asyncio.wait_for(
        runner().run(req(env={"STUB_BIG_LINE": "1"}), _noop), 10)
    assert res.resume_id == "stub-claude-1"
    assert res.text == "BIG" * 40000


async def test_run_survives_stderr_flood():
    r = runner()
    res = await asyncio.wait_for(
        r.run(req(env={"STUB_STDERR_FLOOD": "1"}), _noop), 10)
    assert res.text == "stub reply"
    assert 0 < len(r.last_stderr_tail) <= 2000


# ---- errors --------------------------------------------------------------

async def test_run_failure_raises_with_stderr():
    with pytest.raises(RunError) as e:
        await runner().run(req(env={"STUB_FAIL": "1"}), _noop)
    assert "stub exploded" in e.value.stderr_tail


async def test_error_result_event_raises_with_its_message():
    with pytest.raises(RunError) as e:
        await runner().run(req(env={"STUB_ERROR_RESULT": "1"}), _noop)
    assert str(e.value) == "boom"


async def test_missing_result_event_is_an_error_even_on_exit_zero():
    # `result` is the only authoritative final text; assistant blocks are not.
    with pytest.raises(RunError, match="no final reply"):
        await runner().run(req(env={"STUB_NO_RESULT": "1"}), _noop)


# ---- cancel --------------------------------------------------------------

async def test_cancel_sends_sigint_first(tmp_path):
    # SIGTERM drops claude's in-progress turn; the driver must interrupt.
    assert ClaudeDriver(FakeSettings()).cancel_signal == signal.SIGINT
    sig_file = tmp_path / "sig"
    started = asyncio.Event()

    async def emit(ev):
        if isinstance(ev, Delta):  # stub trap is armed before any output
            started.set()

    r = runner()
    task = asyncio.create_task(r.run(req(env={
        "STUB_TRAP_INT": "1", "STUB_DELAY": "30",
        "STUB_SIGNAL_FILE": str(sig_file)}), emit))
    await asyncio.wait_for(started.wait(), 10)
    await asyncio.wait_for(r.cancel(), 10)
    with pytest.raises(RunError):  # stub exits 130 and never sends `result`
        await asyncio.wait_for(task, 10)
    assert sig_file.read_text().strip() == "int"


async def test_cancel_kills_whole_process_group_promptly():
    r = runner()
    started = time.monotonic()
    task = asyncio.create_task(
        r.run(req("slow job", env={"STUB_DELAY": "30"}), _noop))
    await asyncio.sleep(0.5)  # stub (and its `sleep` child) are running
    pgid = r.pid
    assert pgid is not None
    os.killpg(pgid, 0)  # group exists before cancel
    await asyncio.wait_for(r.cancel(), 10)
    with pytest.raises(RunError):
        await asyncio.wait_for(task, 10)
    assert time.monotonic() - started < 10  # well under the 30 s stub delay
    deadline = time.monotonic() + 3
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break  # leader and every child (the stub's `sleep`) are gone
        assert time.monotonic() < deadline, "process group survived cancel"
        await asyncio.sleep(0.05)


# ---- argv / policies -----------------------------------------------------

async def test_argv_has_headless_streaming_flags(tmp_path):
    argv = await _argv(tmp_path, text="do the thing")
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv and "--include-partial-messages" in argv
    assert "--resume" not in argv and "--permission-mode" not in argv


async def test_text_is_last_after_double_dash(tmp_path):
    argv = await _argv(tmp_path, text="do the thing", resume="s1",
                       policy="edits", model="m",
                       settings=FakeSettings(runtime_args=["--x", "y"]))
    assert argv[-2:] == ["--", "do the thing"]
    assert argv.count("--") == 1


@pytest.mark.parametrize("text", [
    "--dangerously-skip-permissions", "--permission-mode bypassPermissions",
    "--permission-mode"])
async def test_flag_like_text_can_only_follow_double_dash(tmp_path, text):
    argv = await _argv(tmp_path, text=text)
    assert argv[-2:] == ["--", text]
    assert text not in argv[:-2]
    assert "--permission-mode" not in argv[:-2]


async def test_resume_adds_resume_flag(tmp_path):
    argv = await _argv(tmp_path, resume="sess-9")
    assert argv[argv.index("--resume") + 1] == "sess-9"


@pytest.mark.parametrize("policy,mode", [
    ("readonly", "plan"),
    ("edits", "acceptEdits"),
    ("full", "bypassPermissions"),
])
async def test_policy_maps_to_permission_mode(tmp_path, policy, mode):
    argv = await _argv(tmp_path, policy=policy)
    assert argv[argv.index("--permission-mode") + 1] == mode


async def test_model_and_runtime_args_reach_argv(tmp_path):
    argv = await _argv(tmp_path, model="m",
                       settings=FakeSettings(runtime_args=["--x", "y"]))
    assert argv[argv.index("--model") + 1] == "m"
    assert argv[-4:] == ["--x", "y", "--", "hello"]  # args before `--`


def test_spec_declares_streaming_and_all_four_policies():
    assert get_runtime("claude") is SPEC
    assert SPEC.streaming is True
    assert SPEC.policies == frozenset(
        {"default", "readonly", "edits", "full"})


# ---- env -----------------------------------------------------------------

async def test_child_env_passes_claude_auth_but_not_adapter_secrets(
        tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN", "adapter-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    env_file = tmp_path / "env"
    await runner().run(req(env={"STUB_ENV_FILE": str(env_file)}), _noop)
    keys = {line.split("=", 1)[0] for line in env_file.read_text().splitlines()}
    assert "ANTHROPIC_API_KEY" in keys
    assert "AGENT_TOKEN" not in keys


# ---- parser (fixture-level; no live binary exists) -----------------------

def test_parser_unknown_tool_result_falls_back_to_generic_name():
    p = ClaudeParser().feed({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "nope", "content": "x"}]}})
    assert p.events == [Activity("tool", "end", "tool")]


def test_parser_text_block_start_resets_but_other_blocks_do_not():
    parser = ClaudeParser()
    start = lambda kind: parser.feed({"type": "stream_event", "event": {  # noqa: E731
        "type": "content_block_start", "index": 0,
        "content_block": {"type": kind}}})
    assert start("text").events == [Delta(reset=True)]
    assert start("tool_use").events == []


def test_parser_error_result_sets_error_and_final_text():
    p = ClaudeParser().feed({"type": "result", "subtype": "error_max_turns",
                             "is_error": True, "result": "", "session_id": "s"})
    assert p.error == "error_max_turns"
    assert p.session_id == "s"
    assert p.final_text == ""


def _user(content):
    return {"type": "user", "message": {"content": content}}


def _assistant(content):
    return {"type": "assistant", "message": {"content": content}}


@pytest.mark.parametrize("obj", [
    _user("plain string content"),
    _user(None),
    _user({"not": "a list"}),
    _user(["str block", 3, None, {"type": "tool_result"}]),
    _assistant("plain string content"),
    _assistant(None),
    _assistant(["str block", 3, None]),
    {"type": "user", "message": None},
    {"type": "user", "message": "text"},
    {"type": "assistant", "message": ["x"]},
    {"type": "stream_event", "event": "nope"},
    {"type": "stream_event", "event": None},
    {"type": "stream_event", "event": {"type": "content_block_start",
                                       "content_block": "text"}},
    {"type": "stream_event", "event": {"delta": ["x"]}},
    {"type": "stream_event", "event": {"delta": {"type": "text_delta",
                                                 "text": None}}},
    {"type": "result", "result": {"a": 1}},
    {"session_id": 12345, "type": "system"},
    {"type": ["list"]},
])
def test_parser_ignores_unexpected_shapes_instead_of_raising(obj):
    p = ClaudeParser().feed(obj)  # must not raise: a crash SIGKILLs the run
    assert p.session_id is None or isinstance(p.session_id, str)
    assert p.error is None or isinstance(p.error, str)
    assert p.final_text is None or isinstance(p.final_text, str)
    assert all(isinstance(getattr(e, "text", ""), str) for e in p.events)


def test_parser_tool_use_with_non_dict_input_has_empty_detail():
    p = ClaudeParser().feed(_assistant([
        {"type": "tool_use", "id": "t", "name": "Bash", "input": "ls"},
        {"type": "tool_use", "id": "u", "name": "Read", "input": None},
        {"type": "tool_use", "id": "v", "name": "Read", "input": ["a"]}]))
    assert p.events == [Activity("tool", "start", "Bash", ""),
                        Activity("tool", "start", "Read", ""),
                        Activity("tool", "start", "Read", "")]


def test_parser_tool_result_after_odd_blocks_still_resolves_name():
    parser = ClaudeParser()
    parser.feed(_assistant(["junk", {"type": "tool_use", "id": "t",
                                     "name": "Grep", "input": {}}]))
    p = parser.feed(_user(["junk", {"type": "tool_result",
                                    "tool_use_id": "t"}]))
    assert p.events == [Activity("tool", "end", "Grep")]


def _detail(name, inp):
    p = ClaudeParser().feed(_assistant([
        {"type": "tool_use", "id": "t", "name": name, "input": inp}]))
    return p.events[0].detail


def test_detail_never_leaks_file_content_or_other_values():
    secret = "SECRET FILE BODY"
    assert _detail("Write", {"file_path": "/w/a.py", "content": secret}) \
        == "/w/a.py"
    assert _detail("Edit", {"file_path": "/w/a.py", "old_string": secret,
                            "new_string": secret}) == "/w/a.py"
    assert _detail("Write", {"content": secret}) == ""
    assert _detail("Mystery", {"a": secret, "b": 1}) == ""


def test_detail_picks_by_key_priority():
    assert _detail("Bash", {"description": "d", "command": "ls -la"}) \
        == "ls -la"
    assert _detail("Read", {"path": "/p", "file_path": "/f"}) == "/f"
    assert _detail("Grep", {"pattern": "foo", "path": "/p"}) == "/p"
    assert _detail("WebSearch", {"query": "q"}) == "q"
    assert _detail("WebFetch", {"url": "http://x", "prompt": "p"}) == "http://x"
    assert _detail("Task", {"description": "d", "prompt": "p"}) == "d"
    assert _detail("Task", {"prompt": "p"}) == "p"


def test_detail_skips_empty_and_non_string_priority_values():
    assert _detail("Bash", {"command": "", "description": "d"}) == "d"
    assert _detail("Bash", {"command": 5, "description": "d"}) == "d"


def test_detail_is_capped_at_120_chars():
    assert _detail("Bash", {"command": "x" * 500}) == "x" * 120
