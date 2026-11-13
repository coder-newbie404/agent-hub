import asyncio
import pathlib

import pytest

from omp_adapter.omp import OmpRunner, OmpRunError

STUB = str(pathlib.Path(__file__).parent / "stub_omp.sh")


async def test_run_parses_session_and_reply():
    r = OmpRunner(STUB)
    sid, reply = await r.run("/tmp", None, "hello")
    assert sid == "stub-session-1"
    assert reply == "stub reply"  # thinking block excluded


async def test_run_handles_oversized_event_line():
    r = OmpRunner(STUB)
    sid, reply = await asyncio.wait_for(
        r.run("/tmp", None, "hello", env={"OMP_STUB_BIG_LINE": "1"}), 10)
    assert sid == "stub-session-1"
    assert reply == "BIG" * 40000


async def test_run_failure_raises_with_stderr():
    r = OmpRunner(STUB)
    with pytest.raises(OmpRunError) as e:
        await r.run("/tmp", None, "hello", env={"OMP_STUB_FAIL": "1"})
    assert "stub exploded" in e.value.stderr_tail


async def test_cancel_kills_process_group():
    r = OmpRunner(STUB)
    task = asyncio.create_task(r.run("/tmp", None, "slow job",
                                     env={"OMP_STUB_DELAY": "10"}))
    await asyncio.sleep(0.3)  # stub is inside its sleep
    await r.cancel()
    with pytest.raises(OmpRunError):
        await task

async def test_run_survives_stderr_flood():
    r = OmpRunner(STUB)
    sid, reply = await asyncio.wait_for(
        r.run("/tmp", None, "hello", env={"OMP_STUB_STDERR_FLOOD": "1"}), 10)
    assert sid == "stub-session-1"
    assert reply == "stub reply"
    assert 0 < len(r.last_stderr_tail) <= 2000

async def test_mid_stream_failure_kills_child_and_raises_typed_error():
    # The exact production failure: a line beyond the (old 64KiB) stream limit
    # raised LimitOverrunError and orphaned the omp child. Now: typed error,
    # child reaped. readline's ValueError wording varies with transport
    # chunking ('Separator is not found…' vs 'Separator is found, but chunk
    # is longer than limit'), so pin the condition, not the message.
    r = OmpRunner(STUB, stream_limit=64 * 1024)
    with pytest.raises(OmpRunError) as e:
        await asyncio.wait_for(
            r.run("/tmp", None, "hello", env={"OMP_STUB_BIG_LINE": "1"}), 10)
    assert "limit" in str(e.value)
    assert r.proc is None


async def test_run_forwards_stream_events_in_order():
    events: list[dict] = []
    r = OmpRunner(STUB)
    sid, reply = await r.run("/tmp", None, "hello",
                             env={"OMP_STUB_STREAM": "1"},
                             on_event=events.append)
    assert sid == "stub-session-1"
    assert reply == "stub stream"
    types = [e["type"] for e in events]
    assert types[0] == "session"
    assert types.index("tool_execution_start") < types.index("tool_execution_end")
    deltas = [e for e in events
              if e.get("type") == "message_update"
              and e["assistantMessageEvent"]["type"] == "text_delta"]
    assert [d["assistantMessageEvent"]["delta"] for d in deltas] == ["stub ", "stream"]


async def test_run_awaits_async_on_event():
    seen: list[str] = []

    async def cb(ev: dict) -> None:
        seen.append(ev["type"])

    r = OmpRunner(STUB)
    sid, reply = await r.run("/tmp", None, "hello",
                             env={"OMP_STUB_STREAM": "1"}, on_event=cb)
    assert (sid, reply) == ("stub-session-1", "stub stream")
    assert "tool_execution_start" in seen


async def test_run_spawns_without_session_dir(tmp_path):
    # Regression: passing --session-dir sent every Discord-driven run to the
    # flat ~/.omp/sessions store, which the omp TUI /resume picker never
    # reads (it reads the per-project agent store under
    # ~/.omp/agent/sessions/<project>/). omp's default storage IS the agent
    # store, so the flag must stay out of the argv entirely.
    args_file = tmp_path / "argv"
    r = OmpRunner(STUB)
    await r.run("/tmp", None, "hello",
                env={"OMP_STUB_ARGS_FILE": str(args_file)})
    argv = args_file.read_text().splitlines()
    assert "--session-dir" not in argv
    assert "--mode=json" in argv and "--cwd" in argv
    # resume passthrough is unchanged
    await r.run("/tmp", "omp-123", "hello",
                env={"OMP_STUB_ARGS_FILE": str(args_file)})
    argv = args_file.read_text().splitlines()
    assert argv[argv.index("-r") + 1] == "omp-123"
