"""End-to-end check: a hub built from source + a real adapter subprocess + a
real agent CLI, driven only through the hub REST API. Run by hand (the name
does not start with `test_`, so pytest never collects it; it calls a live LLM):

    cd agent/local-adapter
    RUNTIME=omp .venv/bin/python tests/e2e_hub.py
    RUNTIME=opencode MODEL=opencode/glm-5.3-flash-free \
        .venv/bin/python tests/e2e_hub.py

Prerequisite: the hub venv, created once from the working repo:
    cd hub/manager && python3 -m venv .venv \
        && .venv/bin/pip install -q -r requirements-dev.txt -c requirements.lock

Environment:
    RUNTIME       omp | opencode (required; also accepts claude)
    MODEL         optional model for the adapter (opencode needs one)
    RUNTIME_BIN   optional binary override (dry runs against a tests/stubs/
                  script cost no LLM call)
    REPLY_MARKER  text the reply must contain (default `pong`)
    ADAPTER_ENV   optional JSON object of extra adapter env (e.g. stub knobs
                  with ENV_PASSTHROUGH='["STUB_*"]')

Flow: hub on 127.0.0.1:8014 (alembic + uvicorn, SQLite in /tmp) -> adapter
(`python -m agent_adapter`) with everything under one scratch dir -> wait for
`GET /agents` to show it online with the right capabilities (`streaming` iff
the runtime streams tokens) -> POST /sessions, POST a `pong` prompt, poll the
reply -> POST /sync/nudge and wait until the hub's journal cursor reaches the
adapter's journal length (the unchanged hub applying events that carry the new
`rt` key) and the session's `omp_session_id` equals the journaled native id
-> long prompt, POST /cancel, expect status `cancelled` and no agent process
-> teardown, delete temp files. Exit non-zero on any failure. Never prints
tokens or environment values.
"""
import json
import os
import pathlib
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[3]
ADAPTER_DIR = ROOT / "agent" / "local-adapter"
HUB_DIR = ROOT / "hub" / "manager"
HUB_PY = HUB_DIR / ".venv" / "bin" / "python"
PORT = 8014
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "e2e-token"
OWNER = "e2e-owner"
STREAMING = {"omp": True, "claude": True, "opencode": False}
LONG_PROMPT = ("Write a very long, detailed essay (at least 3000 words) on "
               "the full history of the Roman Empire, one section per "
               "emperor.")


class Fail(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)
    print(f"ok: {msg}", flush=True)


def api(method, path, body=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"authorization": f"Bearer {TOKEN}",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")[:300]


def poll(fn, what, timeout):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = fn()
        except Exception as exc:  # hub not up yet, etc.
            last = exc
        if last and not isinstance(last, Exception):
            return last
        time.sleep(0.5)
    raise Fail(f"timed out after {timeout}s waiting for {what} (last: {last!r})")


def agent_pids(scratch: str) -> list[int]:
    """Processes whose cwd or argv points into the scratch workspace: the
    agent CLI (or its stub) and anything it spawned."""
    me, found = os.getpid(), []
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            cwd = os.readlink(entry / "cwd")
            argv = (entry / "cmdline").read_bytes().decode(errors="replace")
        except OSError:
            continue
        if cwd.startswith(scratch) or scratch in argv:
            found.append(int(entry.name))
    return found


def spawn(argv, cwd, env, log):
    return subprocess.Popen(argv, cwd=cwd, env=env, stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True)


def stop(proc):
    if proc is None or proc.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(5)
            return
        except subprocess.TimeoutExpired:
            continue


def journal_state(journal: pathlib.Path):
    lines = [json.loads(line) for line in journal.read_text().splitlines()
             if line.strip()]
    instance = next((e["instance"] for e in lines
                     if e["kind"] == "journal_started"), None)
    return lines, instance


def main() -> int:
    runtime = os.environ.get("RUNTIME", "")
    if runtime not in STREAMING:
        print(f"RUNTIME must be one of {sorted(STREAMING)}", file=sys.stderr)
        return 2
    if not HUB_PY.exists():
        print(f"hub venv missing: {HUB_PY} (see the module docstring)",
              file=sys.stderr)
        return 2
    marker = os.environ.get("REPLY_MARKER", "pong")
    scratch = pathlib.Path(tempfile.mkdtemp(prefix=f"e2e-{runtime}-")).resolve()
    work = scratch / "ws"
    work.mkdir()
    db = pathlib.Path(f"/tmp/e2e-{runtime}.db")
    hub = adapter = None
    hub_log = open(scratch / "hub.log", "w")
    adapter_log = open(scratch / "adapter.log", "w")
    ok = False
    try:
        hub_env = {**os.environ, "ADMIN_TOKEN": TOKEN,
                   "SQLITE_PATH": str(db)}
        subprocess.run([str(HUB_PY), "-m", "alembic", "upgrade", "head"],
                       cwd=HUB_DIR, env=hub_env, check=True, stdout=hub_log,
                       stderr=subprocess.STDOUT)
        hub = spawn([str(HUB_PY), "-m", "uvicorn", "app.main:app",
                     "--host", "127.0.0.1", "--port", str(PORT)],
                    HUB_DIR, hub_env, hub_log)
        poll(lambda: api("GET", "/health")[0] == 200, "hub /health", 30)
        print("hub up", flush=True)

        journal_dir = scratch / "journal"
        agent_id = f"e2e-{runtime}"
        env = {**os.environ,
               "RUNTIME": runtime, "MANAGER_URL": f"ws://127.0.0.1:{PORT}/ws/agent",
               "AGENT_TOKEN": TOKEN, "DEFAULT_WORKSPACE": str(work),
               "ALLOWED_ROOT": str(scratch), "AGENT_ID": agent_id,
               "JOURNAL_DIR": str(journal_dir),
               "STATE_FILE": str(scratch / "state.json"),
               "DELEGATION_DIR": str(scratch / "delegations"),
               "ADAPTER_CONFIG": ""}
        if os.environ.get("MODEL"):
            env["MODEL"] = os.environ["MODEL"]
        if os.environ.get("RUNTIME_BIN"):
            env["RUNTIME_BIN"] = os.environ["RUNTIME_BIN"]
        env.update(json.loads(os.environ.get("ADAPTER_ENV", "{}")))
        adapter = spawn([str(ADAPTER_DIR / ".venv" / "bin" / "python"),
                         "-m", "agent_adapter"], ADAPTER_DIR, env, adapter_log)

        # (c) online with the right capabilities
        def listed():
            status, agents = api("GET", "/agents")
            return next((a for a in agents if a["id"] == agent_id
                         and a["online"]), None) if status == 200 else None
        agent = poll(listed, "adapter online in GET /agents", 30)
        caps = agent["capabilities"]
        caps = json.loads(caps) if isinstance(caps, str) else caps
        check(("streaming" in caps) is STREAMING[runtime],
              f"{runtime} {'advertises' if STREAMING[runtime] else 'does not advertise'}"
              f" streaming (capabilities: {','.join(caps)})")
        check("owns_sessions" in caps, "owns_sessions advertised")

        # (d) session + prompt + reply
        status, sess = api("POST", "/sessions",
                           {"agent_id": agent_id, "owner_id": OWNER,
                            "workspace": str(work)})
        check(status == 201, "POST /sessions -> 201")
        sid = sess["id"]
        status, _ = api("POST", f"/sessions/{sid}/messages",
                        {"text": "Reply with exactly the single word: pong"})
        check(status == 200, "POST message accepted")

        def reply():
            status, msgs = api("GET", f"/sessions/{sid}/messages")
            if status != 200:
                return None
            return next((m for m in msgs if m["role"] == "assistant"
                         and marker in m["content"].lower()), None)
        poll(reply, f"assistant reply containing {marker!r}", 120)
        check(True, f"assistant reply contains {marker!r}")

        # journal: runtime-native id and the `rt` key
        journal = journal_dir / "journal.jsonl"
        events, instance = journal_state(journal)
        upserts = [e for e in events if e["kind"] == "session_upserted"
                   and e.get("omp")]
        check(bool(upserts), "adapter journaled a native session id")
        native = upserts[-1]["omp"]
        check(upserts[-1].get("rt") == runtime,
              f"journal session_upserted carries rt={runtime}")

        # hub applies the journal (live proof: unchanged hub, new `rt` key)
        status, nudged = api("POST", "/sync/nudge")
        check(status == 200 and nudged == {"nudged": 1}, "POST /sync/nudge -> nudged 1")

        def synced():
            lines = len(journal_state(journal)[0])
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            try:
                row = con.execute("SELECT instance, cursor FROM sync_state "
                                  "WHERE agent_id=?", (agent_id,)).fetchone()
            finally:
                con.close()
            return row == (instance, lines)
        poll(synced, "hub sync cursor == adapter journal length", 30)
        check(True, "hub applied the whole journal (cursor == journal length)")

        def native_matches():
            status, row = api("GET", f"/sessions/{sid}")
            return status == 200 and row["omp_session_id"] == native
        poll(native_matches, "hub omp_session_id == journaled native id", 30)
        check(True, "hub omp_session_id == journaled native id")
        status, rows = api("GET", "/sessions")
        check(status == 200 and [r["id"] for r in rows] == [sid],
              "journal sync created no ghost session")

        # (e) cancel a long run
        status, _ = api("POST", f"/sessions/{sid}/messages",
                        {"text": LONG_PROMPT})
        check(status == 200, "long prompt accepted")
        poll(lambda: agent_pids(str(scratch)), "agent process running", 60)
        status, res = api("POST", f"/sessions/{sid}/cancel")
        check(status == 200 and res == {"cancelled": True},
              "POST /cancel -> cancelled true")

        def cancelled():
            status, row = api("GET", f"/sessions/{sid}")
            return status == 200 and row["status"] == "cancelled"
        poll(cancelled, "session status cancelled", 30)
        check(True, "session status is cancelled")
        poll(lambda: not agent_pids(str(scratch)) or None,
             "agent process gone", 15)
        check(True, "no agent process remains")
        ok = True
    except Fail as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        for name in ("adapter.log", "hub.log"):
            tail = (scratch / name).read_text(errors="replace")[-1500:]
            print(f"--- {name} tail ---\n{tail}", file=sys.stderr)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        stop(adapter)
        stop(hub)
        for pid in agent_pids(str(scratch)):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        hub_log.close()
        adapter_log.close()
        shutil.rmtree(scratch, ignore_errors=True)
        for suffix in ("", "-wal", "-shm", "-journal"):
            pathlib.Path(f"{db}{suffix}").unlink(missing_ok=True)
    print("E2E PASS" if ok else "E2E FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
