"""The daemon supervisor must survive crashes and keep the agent online."""
import asyncio

import pytest

import agent_adapter.__main__ as entry
from agent_adapter.config import Settings


def test_run_restarts_after_crash_and_exits_on_interrupt(monkeypatch):
    calls = []
    sleeps = []

    def fake_main():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("env-level teardown")
        raise KeyboardInterrupt

    monkeypatch.setattr(entry, "main", fake_main)
    monkeypatch.setattr(entry.time, "sleep", sleeps.append)
    try:
        entry.run()
        raised = False
    except KeyboardInterrupt:
        raised = True
    assert raised
    assert len(calls) == 2
    assert sleeps == [5]


def test_run_does_not_restart_on_deliberate_exit(monkeypatch):
    """A config refusal (SystemExit) must reach the process exit code, not
    spin in the crash-restart loop."""
    def fake_main():
        raise SystemExit("refusing to start")

    monkeypatch.setattr(entry, "main", fake_main)
    monkeypatch.setattr(entry.time, "sleep",
                        lambda s: pytest.fail("supervisor restarted"))
    with pytest.raises(SystemExit, match="refusing to start"):
        entry.run()


def test_main_refuses_to_start_on_legacy_data(monkeypatch):
    monkeypatch.setattr(entry, "Settings", lambda: object())
    monkeypatch.setattr(entry, "legacy_path_error",
                        lambda settings: "found legacy adapter data")
    monkeypatch.setattr(entry, "AdapterAgent", lambda *a: pytest.fail("started"))
    with pytest.raises(SystemExit, match="found legacy adapter data"):
        asyncio.run(entry.main())


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Hermetic process env for the real Settings(): no .env, no toml."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADAPTER_CONFIG", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    for name, value in {"MANAGER_URL": "wss://x", "AGENT_TOKEN": "t",
                        "DEFAULT_WORKSPACE": str(tmp_path),
                        "RUNTIME": "omp"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(entry, "AdapterAgent",
                        lambda *a: pytest.fail("adapter started"))
    monkeypatch.setattr(entry.time, "sleep",
                        lambda s: pytest.fail("supervisor retried"))
    return monkeypatch


def test_unknown_runtime_exits_instead_of_crash_looping(env):
    env.setenv("RUNTIME", "bogus")
    with pytest.raises(SystemExit) as ei:
        entry.run()
    msg = str(ei.value.code)
    assert "unknown runtime 'bogus'" in msg
    for name in ("claude", "omp", "opencode"):
        assert name in msg


def test_unsupported_policy_exits_with_supported_list(env):
    env.setenv("POLICY", "bogus")
    with pytest.raises(SystemExit) as ei:
        entry.run()
    msg = str(ei.value.code)
    assert "supports policies" in msg
    for policy in ("default", "edits", "full", "readonly"):
        assert policy in msg


def test_missing_required_setting_exits_naming_it(env):
    env.delenv("MANAGER_URL")
    with pytest.raises(SystemExit) as ei:
        entry.run()
    assert "manager_url" in str(ei.value.code)


def test_runtime_crash_after_valid_config_is_still_retried(env, tmp_path):
    sleeps = []
    env.setattr(entry.time, "sleep", sleeps.append)
    env.setenv("JOURNAL_DIR", str(tmp_path / "j"))
    env.setenv("STATE_FILE", str(tmp_path / "s.json"))

    class Boom:
        def __init__(self, *a):
            if sleeps:
                raise KeyboardInterrupt
            raise RuntimeError("env-level teardown")

    env.setattr(entry, "AdapterAgent", Boom)
    with pytest.raises(KeyboardInterrupt):
        entry.run()
    assert sleeps == [5]
