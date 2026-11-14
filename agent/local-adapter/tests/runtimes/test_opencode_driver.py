import asyncio
import json
import pathlib
from dataclasses import dataclass, field

import pytest

from agent_adapter.runtimes import get_runtime
from agent_adapter.runtimes.base import (Activity, Delta, RunError,
                                         RunRequest, RunResult)
from agent_adapter.runtimes.opencode import POLICIES, OpencodeParser

HERE = pathlib.Path(__file__).parent.parent
STUB = str(HERE / "stubs" / "stub_opencode.sh")
FIXTURES = HERE / "fixtures" / "opencode"
SPEC = get_runtime("opencode")


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
    return SPEC.make_runner(FakeSettings(**kw))


def req(text="hello", resume=None, **kw):
    return RunRequest("/tmp", resume, text, **kw)


def parse_fixture(name):
    """Feed a captured stdout through the parser; aggregate like the runner."""
    parser = OpencodeParser()
    events, session_id, final, error = [], None, None, None
    for line in (FIXTURES / f"{name}.ndjson").read_text().splitlines():
        p = parser.feed(json.loads(line))
        events += p.events
        session_id = p.session_id or session_id
        final = p.final_text if p.final_text is not None else final
        error = p.error or error
    return events, session_id, final, error


def test_spec_declares_no_streaming_and_two_policies():
    assert SPEC.name == "opencode"
    assert SPEC.streaming is False
    assert SPEC.policies == {"default", "full"} == set(POLICIES)


# ---- parser against the REAL captured output (opencode 1.18.29) ----------

def test_basic_fixture_session_final_text_and_whole_part():
    events, sid, final, error = parse_fixture("basic")
    assert sid == "ses_e26d7687bffeotUjHCEFeOpF0H"
    assert final == "pong"
    assert events == [Delta(reset=True), Delta(text="pong")]
    assert error is None


def test_resume_fixture_keeps_the_same_session_id():
    _, sid, final, _ = parse_fixture("resume")
    assert sid == parse_fixture("basic")[1]
    assert final == "pong"


def test_tooluse_fixture_one_tool_then_last_text_wins():
    events, sid, final, error = parse_fixture("tooluse")
    assert sid == "ses_e26d73c0effe6J3UoAMg9Sconb"
    assert events == [
        Activity("tool", "start", "read", "/tmp/cap-ws/hello.txt"),
        Activity("tool", "end", "read"),
        Delta(reset=True),
        Delta(text='hello.txt contains a single line reading '
                   '"hello from the scratch workspace".'),
    ]
    assert final == ('hello.txt contains a single line reading '
                     '"hello from the scratch workspace".')
    assert error is None


def test_error_fixture_surfaces_the_error_message():
    events, sid, final, error = parse_fixture("error")
    assert sid == "ses_e26d633ceffe3PCpD5TUk6pYvo"
    assert error == "Unexpected server error. Check server logs for details."
    assert events == [] and final is None


def test_edit_fixture_text_follows_completed_write_tool():
    events, _, final, error = parse_fixture("edit-noauto")
    assert events[0] == Activity("tool", "start", "write",
                                 "ok /tmp/cap-ws/created.txt")  # one line
    assert events[1] == Activity("tool", "end", "write")
    assert final == "Created /tmp/cap-ws/created.txt with the word `ok`."
    assert error is None


def test_rejected_permission_fixture_has_activity_and_no_final_text():
    events, sid, final, error = parse_fixture("edit-external-noauto")
    assert sid == "ses_e26d68646ffeJ8RCV2f67p2URc"
    assert [type(e) for e in events] == [Activity, Activity]
    assert events[0].tool == "write"
    assert final is None and error is None


def test_parser_ignores_lifecycle_events():
    p = OpencodeParser().feed({"type": "step_finish", "sessionID": "s",
                               "part": {"reason": "stop"}})
    assert p == type(p)(session_id="s")


def test_error_without_message_falls_back_to_name():
    p = OpencodeParser().feed({"type": "error", "sessionID": "s",
                               "error": {"name": "UnknownError"}})
    assert p.error == "UnknownError"


# ---- process behavior through the stub -----------------------------------

async def test_run_parses_session_and_reply():
    res = await runner().run(req(), _noop)
    assert res == RunResult(resume_id="stub-opencode-1", text="stub reply")


async def test_run_emits_tool_activity_and_whole_text_in_order():
    events = []

    async def emit(ev):
        events.append(ev)

    res = await runner().run(req(env={"STUB_STREAM": "1"}), emit)
    assert res == RunResult("stub-opencode-1", "stub stream")
    assert events == [
        Activity("tool", "start", "bash", "echo hi"),
        Activity("tool", "end", "bash"),
        Delta(reset=True),
        Delta(text="stub stream"),
    ]


async def test_error_event_becomes_run_error():
    with pytest.raises(RunError, match="Unexpected server error"):
        await runner().run(req(env={"STUB_ERROR_EVENT": "1"}), _noop)


async def test_rejected_permission_is_a_failure_with_stderr_evidence():
    # exit 0 with no final text: require_final_text turns it into an error and
    # the stderr tail tells the operator why.
    with pytest.raises(RunError, match="no final reply") as e:
        await runner().run(req(env={"STUB_REJECT": "1"}), _noop)
    assert "auto-rejecting" in e.value.stderr_tail


async def test_run_failure_raises_with_stderr():
    with pytest.raises(RunError) as e:
        await runner().run(req(env={"STUB_FAIL": "1"}), _noop)
    assert "stub exploded" in e.value.stderr_tail


async def test_run_handles_oversized_event_line():
    res = await asyncio.wait_for(
        runner().run(req(env={"STUB_BIG_LINE": "1"}), _noop), 10)
    assert res.text == "BIG" * 40000


async def test_run_survives_stderr_flood():
    r = runner()
    res = await asyncio.wait_for(
        r.run(req(env={"STUB_STDERR_FLOOD": "1"}), _noop), 10)
    assert res == RunResult("stub-opencode-1", "stub reply")
    assert 0 < len(r.last_stderr_tail) <= 2000


async def test_cancel_kills_process_group():
    r = runner()
    task = asyncio.create_task(
        r.run(req("slow job", env={"STUB_DELAY": "10"}), _noop))
    await asyncio.sleep(0.3)
    await r.cancel()
    with pytest.raises(RunError):
        await task


# ---- argv ------------------------------------------------------------------

async def _argv(tmp_path, *, settings=None, **kw):
    args_file = tmp_path / "argv"
    r = runner(**(settings or {}))
    await r.run(req(env={"STUB_ARGS_FILE": str(args_file)}, **kw), _noop)
    return args_file.read_text().splitlines()


async def test_default_argv(tmp_path):
    argv = await _argv(tmp_path, text="do it")
    assert argv[:4] == ["run", "do it", "--format", "json"]
    assert argv[argv.index("--dir") + 1] == "/tmp"
    for absent in ("-s", "-m", "--auto"):
        assert absent not in argv


async def test_resume_model_and_runtime_args_reach_argv(tmp_path):
    argv = await _argv(tmp_path, resume="ses_abc", model="opencode/m-1",
                       settings={"runtime_args": ["--foo", "bar"]})
    assert argv[argv.index("-s") + 1] == "ses_abc"
    assert argv[argv.index("-m") + 1] == "opencode/m-1"
    assert argv[-2:] == ["--foo", "bar"]


async def test_full_policy_adds_auto_and_only_full(tmp_path):
    assert "--auto" in await _argv(tmp_path, policy="full")
    assert "--auto" not in await _argv(tmp_path, policy="default")


async def test_error_visibility_flags_are_requested(tmp_path):
    # Without --print-logs a bad model yields only a generic "Unexpected
    # server error"; the real cause is on stderr only with these flags.
    argv = await _argv(tmp_path)
    assert argv[argv.index("--print-logs"):][:3] == [
        "--print-logs", "--log-level", "ERROR"]
