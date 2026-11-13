import asyncio
import gc
from contextlib import suppress

from websockets.exceptions import ConnectionClosed

from template_agent.__main__ import serve
from template_agent.agent import Agent, Settings
from template_agent.protocol import Envelope


class FakeWS:
    """Appends sent envelopes; suspends once per send like a real socket."""

    def __init__(self):
        self.sent = []

    async def send(self, raw):
        await asyncio.sleep(0)
        self.sent.append(Envelope.model_validate_json(raw))


def make_agent():
    return Agent(Settings(manager_url="ws://x", agent_token="t", _env_file=None))


async def _drain(agent, sid):
    task = agent._runs.pop(sid)
    with suppress(asyncio.CancelledError):
        await task


def test_message_streams_reply_and_completes():
    agent, ws = make_agent(), FakeWS()
    env = Envelope(type="session.message", session_id="s1",
                   payload={"text": "hi"})

    async def scenario():
        await agent.handle(env, ws)
        await _drain(agent, "s1")

    asyncio.run(scenario())
    kinds = [e.type for e in ws.sent]
    assert kinds == ["session.delta", "agent.message", "session.completed"]
    assert ws.sent[1].payload["text"] == agent.settings.reply


def test_cancel_acks_and_marks_run_failed():
    agent, ws = make_agent(), FakeWS()

    async def scenario():
        await agent.handle(Envelope(type="session.message", session_id="s2",
                                    payload={"text": "x"}), ws)
        await asyncio.sleep(0)          # run task starts, first send in flight
        await agent.handle(Envelope(type="session.cancel",
                                    session_id="s2"), ws)
        await _drain(agent, "s2")

    asyncio.run(scenario())
    kinds = [e.type for e in ws.sent]
    assert "session.cancel_ack" in kinds
    failed = [e for e in ws.sent if e.type == "session.failed"]
    assert failed and failed[0].payload == {"reason": "cancelled"}
    assert "session.completed" not in kinds


def test_cancel_without_run_still_acks():
    agent, ws = make_agent(), FakeWS()
    asyncio.run(agent.handle(Envelope(type="session.cancel",
                                      session_id="s3"), ws))
    assert [e.type for e in ws.sent] == ["session.cancel_ack"]


class SlowTeardownWS(FakeWS):
    """The first send takes `delay` extra loop turns to unwind when cancelled
    (a real socket's send awaits cleanup), so the superseded run reaches its
    cancel handler after the new run has been cancelled."""

    def __init__(self, delay):
        super().__init__()
        self.delay, self.calls = delay, 0

    async def send(self, raw):
        self.calls += 1
        slow = self.calls == 1
        try:
            await super().send(raw)
        except asyncio.CancelledError:
            if slow:
                for _ in range(self.delay):
                    await asyncio.sleep(0)
            raise


def test_supersede_then_cancel_hub_cancelled_run_never_completes():
    """After supersede, two runs of one session coexist; whichever unwinds
    first must not consume the other's hub-cancel marker, or the other run
    streams a reply for a session the hub cancelled."""
    agent, ws = make_agent(), SlowTeardownWS(delay=3)

    async def scenario():
        await agent.handle(Envelope(type="session.message", session_id="s4",
                                    payload={"text": "a"}), ws)
        await asyncio.sleep(0)          # run 1 starts, first send in flight
        first = agent._runs["s4"]
        await agent.handle(Envelope(type="session.message", session_id="s4",
                                    payload={"text": "b"}), ws)
        await asyncio.sleep(0)          # run 1 starts unwinding, run 2 starts
        second = agent._runs["s4"]
        assert second is not first
        await agent.handle(Envelope(type="session.cancel",
                                    session_id="s4"), ws)
        for task in (first, second):
            with suppress(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
    kinds = [e.type for e in ws.sent]
    assert "session.completed" not in kinds
    assert "agent.message" not in kinds
    assert kinds.count("session.failed") == 2      # superseded + cancelled


class ConnWS:
    """One hub connection: yields queued frames, waits until `expect` run
    sends are blocked in flight, then 'closes'. After close, send raises
    ConnectionClosed like a dead socket. `mode` for the run sends:
    "hang"  - block forever (only cancellation ends them);
    "close" - block until the socket closes, then raise ConnectionClosed."""

    def __init__(self, agent, frames, mode, expect):
        self.agent, self.frames, self.mode = agent, list(frames), mode
        self.expect, self.blocked = expect, 0
        self.sent, self.closed, self.runs = [], False, []
        self.all_blocked = asyncio.Event()
        self.close_evt = asyncio.Event()

    async def send(self, raw):
        if self.closed:
            raise ConnectionClosed(None, None)
        await asyncio.sleep(0)
        if "agent.register" not in raw:
            self.blocked += 1
            if self.blocked >= self.expect:
                self.all_blocked.set()
            if self.mode == "hang":
                await asyncio.Event().wait()
            await self.close_evt.wait()
            raise ConnectionClosed(None, None)
        self.sent.append(Envelope.model_validate_json(raw))

    async def __aiter__(self):
        for raw in self.frames:
            yield raw
        self.runs = list(self.agent._runs.values())
        await self.all_blocked.wait()
        self.closed = True
        self.close_evt.set()
        for _ in range(3):              # let "close"-mode runs die on it
            await asyncio.sleep(0)


def _serve_until_disconnect(agent, ws):
    """Run serve() to disconnect; return exceptions the loop reported as
    unretrieved/unhandled (Task.__del__ reports them on gc)."""
    reported = []

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, ctx: reported.append(ctx))
        await serve(ws, agent, agent.settings)

    asyncio.run(scenario())
    gc.collect()
    return reported


def _message_frames(*sids):
    return [Envelope(type="session.message", session_id=sid,
                     payload={"text": "x"}).model_dump_json() for sid in sids]


def test_disconnect_cancels_and_reaps_inflight_runs():
    agent = make_agent()
    ws = ConnWS(agent, _message_frames("a", "b"), mode="hang", expect=2)
    reported = _serve_until_disconnect(agent, ws)
    assert len(ws.runs) == 2
    assert all(t.done() and t.cancelled() for t in ws.runs)
    assert agent._runs == {}
    assert reported == []
    # register went out; the dead socket got no failure/completion frames
    assert [e.type for e in ws.sent] == ["agent.register"]


def test_disconnect_retrieves_runs_and_heartbeat_that_died_on_the_socket(
        monkeypatch):
    """The run and the heartbeat both die with ConnectionClosed; neither may
    be left as a 'Task exception was never retrieved'."""
    monkeypatch.setattr("template_agent.__main__.HEARTBEAT_S", 0)
    agent = make_agent()
    ws = ConnWS(agent, _message_frames("a"), mode="close", expect=1)
    reported = _serve_until_disconnect(agent, ws)
    assert len(ws.runs) == 1 and ws.runs[0].done()
    assert isinstance(ws.runs[0].exception(), ConnectionClosed)
    assert agent._runs == {}
    assert reported == []


def test_session_create_acks_with_matching_ids():
    """The hub awaits a reply with the same session_id + request_id; any
    other pairing times out as 'agent unreachable'."""
    agent, ws = make_agent(), FakeWS()
    asyncio.run(agent.handle(Envelope(type="session.create",
                                      request_id="req_c", session_id="s4",
                                      payload={"workspace": ""}), ws))
    assert len(ws.sent) == 1
    ack = ws.sent[0]
    assert (ack.type, ack.session_id, ack.request_id) == (
        "session.waiting", "s4", "req_c")
    assert ack.payload == {"state": "created"}


def test_cancel_ack_echoes_the_cancel_request_id():
    agent, ws = make_agent(), FakeWS()
    asyncio.run(agent.handle(Envelope(type="session.cancel",
                                      request_id="req_x",
                                      session_id="s5"), ws))
    ack = ws.sent[0]
    assert (ack.type, ack.session_id, ack.request_id) == (
        "session.cancel_ack", "s5", "req_x")


def test_run_frames_echo_the_message_request_id():
    """agent.message carries the id into the hub transcript; every frame of
    the run (completed or cancelled) is tied to the originating message."""
    agent, ws = make_agent(), FakeWS()

    async def scenario():
        await agent.handle(Envelope(type="session.message",
                                    request_id="req_a", session_id="s6",
                                    payload={"text": "x"}), ws)
        await _drain(agent, "s6")
        await agent.handle(Envelope(type="session.message",
                                    request_id="req_b", session_id="s6",
                                    payload={"text": "y"}), ws)
        await asyncio.sleep(0)
        await agent.handle(Envelope(type="session.cancel",
                                    request_id="req_k",
                                    session_id="s6"), ws)
        await _drain(agent, "s6")

    asyncio.run(scenario())
    by_req = {}
    for e in ws.sent:
        by_req.setdefault(e.request_id, []).append(e.type)
    assert by_req["req_a"] == ["session.delta", "agent.message",
                               "session.completed"]
    assert by_req["req_k"] == ["session.cancel_ack"]
    assert by_req["req_b"][-1] == "session.failed"
    assert None not in by_req


class _StopLoop(BaseException):
    """Ends the reconnect loop deterministically; BaseException so no
    `except Exception` form of the loop can swallow it."""


def _drive_main(monkeypatch, script):
    """Run main() against a scripted hub. Each script item is an exception
    instance (connect fails with it) or "clean" (connect succeeds and the hub
    closes cleanly at once). After the script the next connect raises
    _StopLoop. Returns (connect_calls, recorded_sleep_delays)."""
    import template_agent.__main__ as entry

    monkeypatch.setenv("AGENT_TOKEN", "t")
    steps = list(script)
    delays, calls = [], []

    class FakeConnect:
        def __init__(self, *a, **kw):
            calls.append(1)

        async def __aenter__(self):
            step = steps.pop(0) if steps else _StopLoop()
            if step != "clean":
                raise step
            return object()

        async def __aexit__(self, *exc):
            return False

    async def fake_serve(ws, agent, settings):
        return None  # hub closed the connection cleanly

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(entry.websockets, "connect", FakeConnect)
    monkeypatch.setattr(entry, "serve", fake_serve)
    monkeypatch.setattr(entry.asyncio, "sleep", fake_sleep)
    try:
        asyncio.run(entry.main())
    except _StopLoop:
        pass
    else:
        raise AssertionError("main() returned")
    return len(calls), delays


def test_reconnect_survives_handshake_failures_with_backoff(monkeypatch):
    """Docker's published port accepts TCP before the hub is up, so a hub
    restart yields InvalidMessage/InvalidStatus (InvalidHandshake, not
    OSError); the agent must keep retrying instead of dying."""
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidMessage, InvalidStatus
    from websockets.http11 import Response

    calls, delays = _drive_main(monkeypatch, [
        InvalidMessage("did not receive a valid HTTP response"),
        InvalidStatus(Response(503, "Service Unavailable", Headers(), b"")),
        ConnectionClosed(None, None),
        OSError("refused"),
    ])
    assert calls == 5            # 4 failures retried, then the stop sentinel
    assert delays == [1, 2, 4, 8]


def test_clean_close_waits_one_second_and_resets_backoff(monkeypatch):
    """A hub that accepts then closes must not be hot-looped; the failure
    backoff restarts from its base after a connection that was established."""
    from websockets.exceptions import InvalidMessage

    calls, delays = _drive_main(monkeypatch, [
        InvalidMessage("x"), InvalidMessage("x"),
        "clean", "clean",
        InvalidMessage("x"),
    ])
    assert calls == 6
    assert delays == [1, 2, 1, 1, 1]
