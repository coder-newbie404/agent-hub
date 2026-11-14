import asyncio
import ctypes
import inspect
import json
import os
import signal

GRACE_SECONDS = 5.0
STDERR_TAIL_BYTES = 2000
STDERR_CHUNK_BYTES = 4096
# omp emits NDJSON: one event per line. Big replies (long file contents,
# large diffs) make single event lines far exceed asyncio's 64 KiB default
# StreamReader limit, which raises
# "Separator is found, but chunk is longer than limit" and fails the run.
STREAM_LIMIT_BYTES = 64 * 1024 * 1024

# Failsafe: when the adapter process dies, the kernel SIGTERMs every omp child
# that asked via PR_SET_PDEATHSIG (set in the child, before exec), so a crash
# can never leave orphaned full-tools omp runs behind.
PR_SET_PDEATHSIG = 1


def _set_pdeathsig() -> None:
    """Runs in the forked child before exec; failures are non-fatal."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(
            PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception:
        pass


class OmpRunError(Exception):
    def __init__(self, message: str, stderr_tail: str = ""):
        super().__init__(message)
        self.stderr_tail = stderr_tail


async def _drain_stderr(stderr: asyncio.StreamReader) -> str:
    """Read stderr to EOF, keeping only the last STDERR_TAIL_BYTES bytes."""
    tail = b""
    while True:
        chunk = await stderr.read(STDERR_CHUNK_BYTES)
        if not chunk:
            break
        tail = (tail + chunk)[-STDERR_TAIL_BYTES:]
    return tail.decode(errors="replace")


class OmpRunner:
    def __init__(self, omp_bin: str,
                 stream_limit: int = STREAM_LIMIT_BYTES):
        self.omp_bin = omp_bin
        self.stream_limit = stream_limit
        self.proc: asyncio.subprocess.Process | None = None
        self.pid: int | None = None  # last spawned child pid; survives run end

    async def run(self, workspace: str, omp_session_id: str | None, text: str,
                  env: dict[str, str] | None = None,
                  on_event=None, on_spawn=None) -> tuple[str, str]:
        """Run one omp turn. `on_event`, when given, receives every parsed
        NDJSON event in stream order (sync callable or async); streaming and
        activity UX are built on it, while the returned reply stays the
        authority from the final assistant message_end. `on_spawn(pid)` fires
        synchronously right after the child exists so callers can journal it."""
        args = [self.omp_bin, "--mode=json", "--cwd", workspace]
        if omp_session_id:
            args += ["-r", omp_session_id]
        args += ["-p", text]
        self.proc = await asyncio.create_subprocess_exec(
            *args, cwd=workspace, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
            preexec_fn=_set_pdeathsig,
            limit=self.stream_limit,
            env={**os.environ, **(env or {})})
        self.pid = self.proc.pid
        if on_spawn is not None:
            on_spawn(self.pid)
        omp_sid: str | None = None
        reply = ""
        assert self.proc.stdout is not None
        assert self.proc.stderr is not None
        stderr_task = asyncio.create_task(_drain_stderr(self.proc.stderr))
        try:
            while True:
                raw = await self.proc.stdout.readline()
                if not raw:
                    break
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if on_event is not None:
                    res = on_event(ev)
                    if inspect.isawaitable(res):
                        await res
                if ev.get("type") == "session":
                    omp_sid = ev.get("id")
                elif (ev.get("type") == "message_end"
                      and ev.get("message", {}).get("role") == "assistant"):
                    reply = "".join(c.get("text", "") for c in
                                    ev["message"].get("content", [])
                                    if c.get("type") == "text")
        except asyncio.CancelledError:
            await self._kill_child()
            raise
        except Exception as exc:
            # Any other mid-stream failure must not orphan a full-tools agent:
            # kill the process group, then fail the run as a typed error.
            await self._kill_child()
            stderr_tail = await stderr_task
            self.last_stderr_tail = stderr_tail
            await self.proc.wait()
            self.proc = None
            raise OmpRunError(f"omp output error: {exc}", stderr_tail) from exc
        stderr_tail = await stderr_task
        self.last_stderr_tail = stderr_tail
        rc = await self.proc.wait()
        self.proc = None
        if rc != 0:
            raise OmpRunError(f"omp exited {rc}", stderr_tail)
        if omp_sid is None:
            raise OmpRunError("no session event in omp output", stderr_tail)
        return omp_sid, reply

    async def _kill_child(self) -> None:
        if self.proc is None or self.proc.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass

    async def cancel(self) -> None:
        proc = self.proc
        if proc is None or proc.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), GRACE_SECONDS)
        except TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
