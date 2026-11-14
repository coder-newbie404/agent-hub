"""Runner contract and the shared JSONL-over-stdout process runner.

A runtime is anything that can answer one prompt in a workspace and stream
progress. `Runner` is the seam the session runtime talks to; most CLIs share
`JsonlProcessRunner` and only supply a `Driver` (argv + a line parser).
"""
from __future__ import annotations

import asyncio
import ctypes
import fnmatch
import json
import logging
import os
import signal
from dataclasses import dataclass, field
from typing import Awaitable, Callable, ClassVar, Mapping, Protocol

logger = logging.getLogger(__name__)

GRACE_SECONDS = 5.0
STDERR_TAIL_BYTES = 2000
STDERR_CHUNK_BYTES = 4096
# One event line can be huge (file contents, diffs); asyncio's 64 KiB default
# StreamReader limit would fail the run with "chunk is longer than limit".
STREAM_LIMIT_BYTES = 64 * 1024 * 1024
PR_SET_PDEATHSIG = 1

# Environment an agent process may inherit from the adapter. Everything else
# (notably AGENT_TOKEN) is dropped; drivers add their auth variables and the
# operator adds `env_passthrough` patterns.
BASE_ENV = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM",
    "TMPDIR", "TZ", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy",
    "https_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
})
BASE_ENV_PREFIXES = ("LC_", "XDG_")


@dataclass(frozen=True)
class RunRequest:
    workspace: str
    resume_id: str | None
    text: str
    policy: str = "default"
    model: str | None = None
    # extra child env (the hub payload's test hook); the CALLER filters it
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RunResult:
    resume_id: str  # runtime-native session id; non-empty on success
    text: str       # authoritative final assistant text


@dataclass(frozen=True)
class Delta:
    text: str = ""
    reset: bool = False  # a new text block starts: clear the preview


@dataclass(frozen=True)
class Activity:
    kind: str                  # "think" | "tool"
    state: str | None = None   # tools: "start" | "end"
    tool: str | None = None
    detail: str | None = None


Event = Delta | Activity


@dataclass
class Parsed:
    events: list[Event] = field(default_factory=list)
    session_id: str | None = None   # None = this line carries no id
    final_text: str | None = None   # None = this line carries no final text
    error: str | None = None        # driver-detected failure


class RunError(Exception):
    def __init__(self, message: str, stderr_tail: str = ""):
        super().__init__(message)
        self.stderr_tail = stderr_tail


# Progress sink. Best-effort: the runner swallows (and debug-logs) any
# exception an emit raises, so a broken sink never fails or kills the run;
# only cancellation propagates.
Emit = Callable[[Event], Awaitable[None]]


class Runner(Protocol):
    pid: int | None
    last_stderr_tail: str

    async def run(self, req: RunRequest, emit: Emit,
                  on_spawn: Callable[[int], None] | None = None
                  ) -> RunResult: ...

    async def cancel(self) -> None: ...  # idempotent: graceful, then hard


class Parser(Protocol):
    def feed(self, obj: dict) -> Parsed: ...


class Driver(Protocol):
    name: ClassVar[str]
    require_final_text: ClassVar[bool]   # no final text on exit 0 => failure
    cancel_signal: ClassVar[int]
    auth_env: ClassVar[tuple[str, ...]]  # fnmatch patterns

    def argv(self, binary: str, req: RunRequest) -> list[str]: ...
    def parser(self) -> Parser: ...      # fresh parser per run


def child_env(environ: Mapping[str, str], *, auth: tuple[str, ...],
              passthrough: list[str], exclude: set[str],
              extra: Mapping[str, str]) -> dict[str, str]:
    """Allow-list the environment handed to an agent process."""
    patterns = (*auth, *passthrough)
    out: dict[str, str] = {}
    for key, value in environ.items():
        if key.upper() in exclude:
            continue
        if (key in BASE_ENV or key.startswith(BASE_ENV_PREFIXES)
                or any(fnmatch.fnmatchcase(key, p) for p in patterns)):
            out[key] = value
    out.update(extra)
    return out


def _set_pdeathsig() -> None:
    """Runs in the forked child before exec; failures are non-fatal. When the
    adapter dies the kernel SIGTERMs every child so none is orphaned."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(
            PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception:
        pass


async def _drain_stderr(stderr: asyncio.StreamReader) -> str:
    tail = b""
    while True:
        chunk = await stderr.read(STDERR_CHUNK_BYTES)
        if not chunk:
            break
        tail = (tail + chunk)[-STDERR_TAIL_BYTES:]
    return tail.decode(errors="replace")


class JsonlProcessRunner:
    """Spawn one CLI per turn, parse its stdout as JSON lines via the driver."""

    def __init__(self, driver: Driver, settings) -> None:
        self.driver = driver
        self.binary = settings.runtime_bin or driver.name
        self.stream_limit = settings.stream_limit_bytes
        self.passthrough = list(settings.env_passthrough)
        # adapter settings never reach the agent (AGENT_TOKEN, MANAGER_URL, ...)
        self.exclude = {n.upper() for n in type(settings).model_fields}
        self.static_env: dict[str, str] = {}
        if getattr(settings, "delegation_enabled", False):
            self.static_env["ADAPTER_DELEGATION_DIR"] = os.path.expanduser(
                settings.delegation_dir)
        self.proc: asyncio.subprocess.Process | None = None
        self.pid: int | None = None  # last spawned child pid; survives run end
        self.last_stderr_tail = ""

    async def run(self, req, emit, on_spawn=None):
        d = self.driver
        env = child_env(os.environ, auth=d.auth_env,
                        passthrough=self.passthrough, exclude=self.exclude,
                        extra={**self.static_env, **dict(req.env)})
        self.proc = await asyncio.create_subprocess_exec(
            *d.argv(self.binary, req), cwd=req.workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, preexec_fn=_set_pdeathsig,
            limit=self.stream_limit, env=env)
        self.pid = self.proc.pid
        if on_spawn is not None:
            on_spawn(self.pid)
        assert self.proc.stdout is not None and self.proc.stderr is not None
        stderr_task = asyncio.create_task(_drain_stderr(self.proc.stderr))
        parser = d.parser()
        session_id: str | None = None
        final: str | None = None
        error: str | None = None
        try:
            while True:
                raw = await self.proc.stdout.readline()
                if not raw:
                    break
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                parsed = parser.feed(obj)
                for event in parsed.events:
                    try:
                        await emit(event)
                    except Exception:  # CancelledError is BaseException
                        logger.debug("emit failed; event dropped",
                                     exc_info=True)
                if parsed.session_id:
                    session_id = parsed.session_id
                if parsed.final_text is not None:
                    final = parsed.final_text
                if parsed.error:
                    error = parsed.error
        except asyncio.CancelledError:
            # Cancelled mid-run (session cancel): end the turn the driver's
            # way (cancel_signal + grace) before SIGKILL. Shielded so a
            # second cancel cannot cut the escalation short; the finally
            # still guarantees the group is killed.
            proc = self.proc
            try:
                await asyncio.shield(self._stop_after_cancel(proc, stderr_task))
            finally:
                await self._kill_child()
            raise
        except Exception as exc:
            # A mid-stream failure must not orphan a full-tools agent.
            await self._kill_child()
            tail = await stderr_task
            self.last_stderr_tail = tail
            await self.proc.wait()
            self.proc = None
            raise RunError(f"{d.name} output error: {exc}", tail) from exc
        tail = await stderr_task
        self.last_stderr_tail = tail
        rc = await self.proc.wait()
        self.proc = None
        if error or rc != 0:
            raise RunError(error or f"{d.name} exited {rc}", tail)
        if session_id is None:
            raise RunError(f"no session id in {d.name} output", tail)
        if d.require_final_text and final is None:
            raise RunError(f"{d.name} produced no final reply", tail)
        return RunResult(session_id, final or "")

    async def _signal_group(self, proc, sig: int) -> bool:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except ProcessLookupError:
            return False
        return True

    async def _graceful_stop(self, proc) -> None:
        """cancel_signal, wait up to GRACE_SECONDS, then SIGKILL the group."""
        if not await self._signal_group(proc, self.driver.cancel_signal):
            return
        try:
            await asyncio.wait_for(proc.wait(), GRACE_SECONDS)
        except TimeoutError:
            await self._signal_group(proc, signal.SIGKILL)
            await proc.wait()

    async def _stop_after_cancel(self, proc, stderr_task) -> None:
        try:
            if proc.returncode is None:
                await self._graceful_stop(proc)
            else:
                await proc.wait()
            # sweep stragglers the leader left behind (pgid == leader pid)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:  # wait_for cancels the drain task itself on timeout
                self.last_stderr_tail = await asyncio.wait_for(
                    stderr_task, GRACE_SECONDS)
            except Exception:
                pass
        finally:
            if self.proc is proc:
                self.proc = None

    async def _kill_child(self) -> None:
        if self.proc is None or self.proc.returncode is not None:
            return
        await self._signal_group(self.proc, signal.SIGKILL)

    async def cancel(self) -> None:
        proc = self.proc
        if proc is None or proc.returncode is not None:
            return
        await self._graceful_stop(proc)
