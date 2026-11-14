import asyncio
import os
import pathlib
import signal
from dataclasses import dataclass, field

import pytest

from agent_adapter.runtimes import UnknownRuntime, base, get_runtime, names
from agent_adapter.runtimes.base import (Delta, JsonlProcessRunner, Parsed,
                                         RunError, RunRequest, RunResult,
                                         child_env)

STUBS = pathlib.Path(__file__).parent.parent / "stubs"
GENERIC = str(STUBS / "stub_generic.sh")

ENV = {
    "PATH": "/bin", "HOME": "/home/x", "LC_ALL": "C.UTF-8",
    "XDG_CONFIG_HOME": "/home/x/.config",
    "AGENT_TOKEN": "secret", "MANAGER_URL": "wss://x", "ALLOWED_ROOT": "/w",
    "FOO_API_KEY": "k", "MY_VAR": "v",
}


def _env(**kw):
    args = dict(auth=(), passthrough=[], exclude=set(), extra={})
    args.update(kw)
    return child_env(ENV, **args)


def test_child_env_keeps_base_and_drops_adapter_secrets():
    out = _env()
    assert {"PATH", "HOME", "LC_ALL", "XDG_CONFIG_HOME"} <= set(out)
    assert not {"AGENT_TOKEN", "MANAGER_URL", "ALLOWED_ROOT"} & set(out)


def test_child_env_auth_and_passthrough_patterns():
    assert "FOO_API_KEY" not in _env()
    assert _env(auth=("*_API_KEY",))["FOO_API_KEY"] == "k"
    assert "MY_VAR" not in _env()
    assert _env(passthrough=["MY_*"])["MY_VAR"] == "v"


def test_child_env_exclude_beats_patterns():
    out = _env(passthrough=["AGENT_*"], exclude={"AGENT_TOKEN"})
    assert "AGENT_TOKEN" not in out


def test_child_env_extra_overrides_and_adds():
    out = _env(extra={"PATH": "/x", "NEW": "1"})
    assert out["PATH"] == "/x" and out["NEW"] == "1"


def test_registry_names_and_lookup():
    assert names() == ["claude", "omp", "opencode"]
    assert get_runtime("omp").name == "omp"


def test_unknown_runtime_lists_available():
    with pytest.raises(UnknownRuntime) as e:
        get_runtime("nope")
    for n in names():
        assert n in str(e.value)


@dataclass
class _Settings:
    runtime_bin: str = GENERIC
    stream_limit_bytes: int = 64 * 1024 * 1024
    env_passthrough: list = field(default_factory=list)
    delegation_enabled: bool = False
    delegation_dir: str = ""
    model_fields = {}


class _Parser:
    def feed(self, obj):
        p = Parsed()
        t = obj.get("type")
        if t == "session":
            p.session_id = obj["id"]
        elif t == "delta":
            p.events.append(Delta(text=obj["text"]))
        elif t == "reply":
            p.final_text = obj["text"]
        elif t == "error":
            p.error = obj["message"]
        return p


class _Driver:
    name = "generic"
    require_final_text = True
    cancel_signal = signal.SIGTERM
    auth_env = ()

    def argv(self, binary, req):
        return [binary, "-p", req.text]

    def parser(self):
        return _Parser()


class _NoFinalParser(_Parser):
    def feed(self, obj):
        p = super().feed(obj)
        p.final_text = None
        return p


class _NoFinalDriver(_Driver):
    def parser(self):
        return _NoFinalParser()


def _req(**kw):
    return RunRequest("/tmp", None, "hi", **kw)


async def _noop(event):
    pass


async def test_generic_runner_happy_path():
    r = JsonlProcessRunner(_Driver(), _Settings())
    assert await r.run(_req(), _noop) == RunResult("stub-generic-1",
                                                   "stub reply")


async def test_missing_final_text_fails_when_driver_requires_it():
    r = JsonlProcessRunner(_NoFinalDriver(), _Settings())
    with pytest.raises(RunError, match="produced no final reply"):
        await r.run(_req(), _noop)


async def test_driver_error_beats_exit_code_message():
    r = JsonlProcessRunner(_Driver(), _Settings())
    with pytest.raises(RunError) as e:
        await r.run(_req(env={"STUB_FAIL": "1"}), _noop)
    assert str(e.value) == "driver says boom"
    assert "stub exploded" in e.value.stderr_tail


async def test_failing_emit_does_not_fail_the_run():
    calls = []

    async def bad_emit(event):
        calls.append(event)
        raise RuntimeError("sink broke")

    r = JsonlProcessRunner(_Driver(), _Settings())
    res = await r.run(_req(), bad_emit)
    assert res == RunResult("stub-generic-1", "stub reply")
    assert calls == [Delta(text="stub "), Delta(text="reply")]  # kept going


async def test_cancelled_emit_still_propagates_and_kills_child():
    async def cancelling_emit(event):
        raise asyncio.CancelledError

    r = JsonlProcessRunner(_Driver(), _Settings())
    with pytest.raises(asyncio.CancelledError):
        await r.run(_req(), cancelling_emit)
    for _ in range(50):  # the killed leader is reaped asynchronously
        try:
            os.killpg(r.pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("child group still alive after cancellation")


async def _gone(pgid, timeout=2.0):
    for _ in range(int(timeout / 0.05)):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.05)
    return False


async def _start_cancellable(driver, env):
    """Start run() against the generic stub; return (runner, task) once the
    stub has armed its signal handling."""
    armed = asyncio.Event()

    async def emit(event):
        if event == Delta(text="armed"):
            armed.set()

    r = JsonlProcessRunner(driver, _Settings())
    task = asyncio.create_task(r.run(_req(env=env), emit))
    await asyncio.wait_for(armed.wait(), 5)
    return r, task


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_task_cancel_delivers_driver_cancel_signal_first(tmp_path, sig):
    class D(_Driver):
        cancel_signal = sig

    sigfile = tmp_path / "sig"
    r, task = await _start_cancellable(
        D(), {"STUB_TRAP_SIGNAL_FILE": str(sigfile), "STUB_DELAY": "30"})
    pgid = r.pid
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sigfile.read_text().strip() == sig.name[3:]  # graceful, not SIGKILL
    assert await _gone(pgid)
    assert r.proc is None


async def test_task_cancel_escalates_to_sigkill_after_grace(monkeypatch):
    monkeypatch.setattr(base, "GRACE_SECONDS", 0.3)
    r, task = await _start_cancellable(
        _Driver(), {"STUB_IGNORE_SIGNALS": "1", "STUB_DELAY": "30"})
    pgid = r.pid
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert loop.time() - t0 >= 0.25  # the signal was given its grace first
    assert await _gone(pgid)
    assert r.proc is None


async def test_second_cancel_cannot_skip_the_group_kill(monkeypatch):
    monkeypatch.setattr(base, "GRACE_SECONDS", 30)
    r, task = await _start_cancellable(
        _Driver(), {"STUB_IGNORE_SIGNALS": "1", "STUB_DELAY": "30"})
    pgid = r.pid
    task.cancel()
    await asyncio.sleep(0.2)  # inside the grace wait
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _gone(pgid)
