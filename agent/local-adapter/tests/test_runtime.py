"""Runtime durability: journal replay/corruption, crash sweep, dedupe,
cancel states. The runtime is the source of truth — these tests pin the
failsafes the hub projection relies on."""
import asyncio
import json
import pathlib
import subprocess

import agent_adapter.runtime as runtime_mod
from agent_adapter.config import Settings
from agent_adapter.runtime import SessionRuntime
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


def make_settings(tmp_path, **kw):
    kw = {"allow_payload_env": True, **kw}
    return Settings(manager_url="wss://x", agent_token="t",
                    default_workspace=str(tmp_path), runtime="omp",
                    runtime_bin=STUB,
                    state_file=str(tmp_path / "state.json"),
                    journal_dir=str(tmp_path / "journal"), **kw)


class Recorder:
    def __init__(self):
        self.envs: list[Envelope] = []

    async def __call__(self, env: Envelope):
        self.envs.append(env)

    def types(self):
        return [e.type for e in self.envs]


async def test_journal_replay_folds_state(tmp_path):
    rt = SessionRuntime(make_settings(tmp_path), Recorder())
    rt.upsert_session("s1", "ws-a")
    rt.journal.run_started("s1", None)
    rt.journal.run_finished("s1", "idle")
    rt.upsert_session("s1", "ws-b")  # rebinding wins on fold

    replayed = SessionRuntime(make_settings(tmp_path), Recorder())
    await replayed.start()
    assert replayed._state["s1"] == {"omp": None, "ws": "ws-b",
                                     "status": "idle"}
    assert replayed._active == {}


async def test_corrupt_journal_tail_truncated_on_replay(tmp_path):
    rt = SessionRuntime(make_settings(tmp_path), Recorder())
    rt.upsert_session("s1", "ws-a")
    rt.upsert_session("s2", "ws-b")
    journal = pathlib.Path(tmp_path / "journal" / "journal.jsonl")
    with open(journal, "ab") as f:  # torn write: half a line, no newline
        f.write(b'{"kind": "sessi')

    replayed = SessionRuntime(make_settings(tmp_path), Recorder())
    await replayed.start()
    assert set(replayed._state) == {"s1", "s2"}
    # the torn line is gone from the file, valid lines all survive
    for line in journal.read_bytes().splitlines():
        json.loads(line)


async def test_startup_sweep_marks_unfinished_failed(tmp_path):
    rt = SessionRuntime(make_settings(tmp_path), Recorder())
    rt.upsert_session("s1", "ws-a")
    rt.journal.run_started("s1", None)  # crash before run_finished

    replayed = SessionRuntime(make_settings(tmp_path), Recorder())
    await replayed.start()
    assert replayed._state["s1"]["status"] == "failed"


async def test_startup_sweep_kills_live_run_group(tmp_path):
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        rt = SessionRuntime(make_settings(tmp_path), Recorder())
        rt.upsert_session("s1", "ws-a")
        rt.journal.run_started("s1", proc.pid)  # pid recorded pre-crash

        replayed = SessionRuntime(make_settings(tmp_path), Recorder())
        await replayed.start()
        assert replayed._state["s1"]["status"] == "failed"
        proc.wait(timeout=5)  # SIGKILLed group: returns, no timeout
        assert proc.returncode == -9
    finally:
        if proc.poll() is None:
            proc.kill()


async def test_duplicate_request_never_respawns(tmp_path, monkeypatch):
    calls = []

    class InstantRunner:
        pid = None
        last_stderr_tail = ""

        async def run(self, req, emit, on_spawn=None):
            calls.append((req.workspace, req.resume_id))
            return RunResult("omp-1", "ok")

        async def cancel(self):
            pass

    use_runner(monkeypatch, InstantRunner)
    send = Recorder()
    rt = SessionRuntime(make_settings(tmp_path), send)
    await rt.start()
    rt.upsert_session("s1", "ws-a")

    assert rt.begin_run("s1", "hi", "req-1", None) == "started"
    await asyncio.wait_for(rt._active["s1"], 5)
    # same request redelivered (at-least-once hub/WS retry): acked, never
    # re-spawned
    assert rt.begin_run("s1", "hi", "req-1", None) == "duplicate"
    assert calls == [("ws-a", None)]
    # and the session is idle again, so a fresh request still starts
    assert rt.begin_run("s1", "hi", "req-2", None) == "started"
    await asyncio.wait_for(rt._active["s1"], 5)


async def test_cancel_states(tmp_path, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockedRunner:
        pid = None
        last_stderr_tail = ""

        async def run(self, req, emit, on_spawn=None):
            started.set()
            await release.wait()
            return RunResult("omp-1", "ok")

        async def cancel(self):
            release.set()
    use_runner(monkeypatch, BlockedRunner)

    send = Recorder()
    rt = SessionRuntime(make_settings(tmp_path), send)
    await rt.start()
    rt.upsert_session("s1", "ws-a")

    assert rt.begin_run("s1", "hi", "req-1", None) == "started"
    await asyncio.wait_for(started.wait(), 5)
    assert rt.cancel("s1") == "accepted"
    await asyncio.wait_for(
        asyncio.gather(rt._active["s1"], return_exceptions=True), 5)
    assert rt.cancel("s1") == "already_finished"
    failed = [e for e in send.envs if e.type == "session.failed"]
    assert len(failed) == 1 and failed[0].payload == {"reason": "cancelled"}


async def test_journal_instance_generated_once_and_survives_replay(tmp_path):
    rt = SessionRuntime(make_settings(tmp_path), Recorder())
    await rt.start()
    inst = rt.instance
    assert inst
    rt2 = SessionRuntime(make_settings(tmp_path), Recorder())
    await rt2.start()
    assert rt2.instance == inst


async def test_message_events_fold_into_transcripts(tmp_path):
    rt = SessionRuntime(make_settings(tmp_path), Recorder())
    await rt.start()
    rt.upsert_session("s1", "/w")
    rt.journal_user_message("s1", "hello", "req_1")
    rt.journal_assistant_message("s1", "world", "req_1")
    rt2 = SessionRuntime(make_settings(tmp_path), Recorder())
    await rt2.start()
    assert [(m["role"], m["content"]) for m in rt2._messages["s1"]] == [
        ("user", "hello"), ("assistant", "world")]
    assert rt2.local_message_count("s1") == 2


async def test_read_from_returns_window_with_stable_offsets(tmp_path):
    rt = SessionRuntime(make_settings(tmp_path), Recorder())
    await rt.start()
    rt.upsert_session("s1", "/w")
    rt.journal_user_message("s1", "a", None)
    rt.journal_user_message("s1", "b", None)
    # layout: 0=journal_started 1=upsert 2="a" 3="b"
    evs, nxt = rt.journal.read_from(2, 1)
    assert len(evs) == 1 and nxt == 3
    assert evs[0]["kind"] == "message" and evs[0]["content"] == "a"
    evs, nxt = rt.journal.read_from(2, 10)
    assert len(evs) == 2 and nxt == 4  # instance + upsert + 2 messages


async def test_merge_hub_messages_skips_known_request_ids(tmp_path):
    rt = SessionRuntime(make_settings(tmp_path), Recorder())
    await rt.start()
    rt.upsert_session("s1", "/w")
    rt.journal_user_message("s1", "hello", "req_1")
    merged = rt.merge_hub_messages("s1", [
        {"id": 1, "role": "user", "content": "hello",
         "request_id": "req_1", "created_at": "2026-09-18T00:00:00"},
        {"id": 2, "role": "assistant", "content": "hi",
         "request_id": "req_2", "created_at": "2026-09-18T00:00:01"},
    ])
    assert merged == 1  # only the unknown one
    assert rt.hub_last_msg("s1") == 2
    assert rt.local_message_count("s1") == 2
    # merged rows are flagged mirrored: the hub skips them when the
    # journal tail is pushed back (echo amplification guard)
    evs, _ = rt.journal.read_from(0, 50)
    msgs = [e for e in evs if e.get("kind") == "message"]
    assert [m["content"] for m in msgs] == ["hello", "hi"]
    assert msgs[0].get("mirrored", False) is False
    assert msgs[1].get("mirrored") is True


async def test_replay_preserves_hub_cursor_across_upserts(tmp_path):
    rt = SessionRuntime(make_settings(tmp_path), Recorder())
    await rt.start()
    rt.upsert_session("s1", "/w")
    rt.journal.hub_cursor("s1", 5)
    # a later run re-journals the session (omp id upsert) — the folded
    # cursor must survive the fresh entry on replay
    rt.journal.session_upserted("s1", "omp-1", "/w", "idle")
    rt2 = SessionRuntime(make_settings(tmp_path), Recorder())
    await rt2.start()
    assert rt2._state["s1"]["hub_last_msg"] == 5

async def test_titled_folds_and_survives_reupsert(tmp_path):
    import agent_adapter.runtime as rmod
    j = rmod.SessionJournal(str(tmp_path / "journal"))
    j.session_upserted("s1", "omp-1", "/w", "idle")
    j.titled("s1", "Fix login flow")
    j.session_upserted("s1", "omp-1", "/w", "idle")  # re-upsert keeps title
    state, _, _ = j.replay()
    assert state["s1"]["title"] == "Fix login flow"


async def test_refresh_titles_journals_changes(tmp_path):
    import sqlite3
    db = tmp_path / "history.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE session_titles (session_id TEXT, title TEXT, "
                "updated_at INTEGER)")
    con.execute("INSERT INTO session_titles VALUES ('omp-1', 'Fix login', 1)")
    con.commit()
    con.close()
    rt = SessionRuntime(make_settings(tmp_path, omp_titles_db=str(db)),
                        Recorder())
    rt.upsert_session("s1", "/w")
    rt._state["s1"]["omp"] = "omp-1"
    assert rt.refresh_titles() == 1
    assert rt._state["s1"]["title"] == "Fix login"
    assert rt.snapshot_rows()[0]["title"] == "Fix login"
    # unchanged title -> no new journal event
    assert rt.refresh_titles() == 0
    # missing titles db is a silent no-op
    rt2 = SessionRuntime(make_settings(
        tmp_path, omp_titles_db=str(tmp_path / "nope.db")), Recorder())
    assert rt2.refresh_titles() == 0


class CapturingRunner:
    """Records each RunRequest; returns a fixed native id."""
    reqs: list = []
    pid = None
    last_stderr_tail = ""

    async def run(self, req, emit, on_spawn=None):
        CapturingRunner.reqs.append(req)
        return RunResult("new-id", "ok")

    async def cancel(self):
        pass


async def _run_once(rt, sid="s1", env=None):
    assert rt.begin_run(sid, "hi", "req-1", env) == "started"
    await asyncio.wait_for(rt._active[sid], 5)


async def test_legacy_journal_entry_without_rt_resumes_as_omp(tmp_path,
                                                              monkeypatch):
    use_runner(monkeypatch, CapturingRunner)
    CapturingRunner.reqs = []
    rt = SessionRuntime(make_settings(tmp_path), Recorder())
    rt.journal.session_upserted("s1", "old-id", "ws-a", "idle")  # no rt
    replayed = SessionRuntime(make_settings(tmp_path), Recorder())
    await replayed.start()
    await _run_once(replayed)
    assert [(r.workspace, r.resume_id) for r in CapturingRunner.reqs] == [
        ("ws-a", "old-id")]


async def test_session_of_another_runtime_starts_fresh(tmp_path, monkeypatch,
                                                       caplog):
    use_runner(monkeypatch, CapturingRunner)
    CapturingRunner.reqs = []
    rt = SessionRuntime(make_settings(tmp_path), Recorder())
    rt.journal.session_upserted("s1", "c-id", "ws-a", "idle", rt="claude")
    replayed = SessionRuntime(make_settings(tmp_path), Recorder())
    await replayed.start()
    with caplog.at_level("WARNING", logger="agent_adapter.runtime"):
        await _run_once(replayed)
    assert [(r.workspace, r.resume_id) for r in CapturingRunner.reqs] == [
        ("ws-a", None)]
    assert "claude" in caplog.text and "c-id" in caplog.text
    # the fresh native session replaces the foreign one and is tagged omp
    assert replayed._state["s1"]["omp"] == "new-id"
    assert replayed._state["s1"]["rt"] == "omp"


async def test_run_journals_runtime_tag(tmp_path, monkeypatch):
    use_runner(monkeypatch, CapturingRunner)
    CapturingRunner.reqs = []
    rt = SessionRuntime(make_settings(tmp_path), Recorder())
    await rt.start()
    rt.upsert_session("s1", "ws-a")
    await _run_once(rt)
    evs, _ = rt.journal.read_from(0, 50)
    upserts = [e for e in evs if e.get("kind") == "session_upserted"]
    assert upserts[-1]["omp"] == "new-id" and upserts[-1]["rt"] == "omp"
    # and the tag survives a restart
    replayed = SessionRuntime(make_settings(tmp_path), Recorder())
    await replayed.start()
    assert replayed._state["s1"]["rt"] == "omp"


async def test_payload_env_reaches_runner_only_when_allowed(tmp_path,
                                                            monkeypatch):
    use_runner(monkeypatch, CapturingRunner)
    CapturingRunner.reqs = []
    for allow in (False, True):
        rt = SessionRuntime(
            make_settings(tmp_path / f"a{allow}", allow_payload_env=allow),
            Recorder())
        await rt.start()
        rt.upsert_session("s1", "ws-a")
        await _run_once(rt, env={"STUB_DELAY": "1"})
    assert [dict(r.env) for r in CapturingRunner.reqs] == [
        {}, {"STUB_DELAY": "1"}]


async def test_failed_stream_send_does_not_fail_the_run(tmp_path,
                                                        monkeypatch):
    class ChattyRunner(CapturingRunner):
        async def run(self, req, emit, on_spawn=None):
            await emit(Delta("x"))
            await emit(Activity("think"))
            return RunResult("new-id", "ok")

    use_runner(monkeypatch, ChattyRunner)
    envs = []

    async def flaky(env):
        if env.type in ("session.delta", "session.activity"):
            raise ConnectionError("hub gone")
        envs.append(env)

    rt = SessionRuntime(make_settings(tmp_path), flaky)
    await rt.start()
    rt.upsert_session("s1", "ws-a")
    await _run_once(rt)
    assert [e.type for e in envs][-2:] == ["agent.message",
                                           "session.completed"]


async def test_refresh_titles_skips_other_runtimes_and_untitled_runtimes(
        tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "history.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE session_titles (session_id TEXT, title TEXT, "
                "updated_at INTEGER)")
    con.execute("INSERT INTO session_titles VALUES ('c-id', 'Foreign', 1)")
    con.commit()
    con.close()
    rt = SessionRuntime(make_settings(tmp_path, omp_titles_db=str(db)),
                        Recorder())
    rt.journal.session_upserted("s1", "c-id", "/w", "idle", rt="claude")
    await rt.start()
    # the id sits in omp's store, but the session belongs to another runtime
    assert rt.refresh_titles() == 0
    assert "title" not in rt._state["s1"]

    monkeypatch.setattr(runtime_mod, "get_runtime",
                        lambda name: FakeSpec(CapturingRunner))
    rt2 = SessionRuntime(make_settings(tmp_path, omp_titles_db=str(db)),
                         Recorder())
    rt2._state["s2"] = {"omp": "c-id", "rt": "omp", "ws": "/w",
                        "status": "idle"}
    assert rt2.refresh_titles() == 0  # FakeSpec has titles=None
