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
        raise SystemExit(78)

    monkeypatch.setattr(entry, "main", fake_main)
    monkeypatch.setattr(entry.time, "sleep",
                        lambda s: pytest.fail("supervisor restarted"))
    with pytest.raises(SystemExit) as ei:
        entry.run()
    assert ei.value.code == 78


def test_main_refuses_to_start_on_legacy_data(monkeypatch, capsys):
    monkeypatch.setattr(entry, "Settings", lambda: object())
    monkeypatch.setattr(entry, "legacy_path_error",
                        lambda settings: "found legacy adapter data")
    monkeypatch.setattr(entry, "AdapterAgent", lambda *a: pytest.fail("started"))
    with pytest.raises(SystemExit) as ei:
        asyncio.run(entry.main())
    assert ei.value.code == 78
    assert "found legacy adapter data" in capsys.readouterr().err


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


def _refused(capsys) -> str:
    with pytest.raises(SystemExit) as ei:
        entry.run()
    assert ei.value.code == 78   # EX_CONFIG: unit uses RestartPreventExitStatus
    return capsys.readouterr().err


def test_unknown_runtime_exits_instead_of_crash_looping(env, capsys):
    env.setenv("RUNTIME", "bogus")
    msg = _refused(capsys)
    assert "unknown runtime 'bogus'" in msg
    for name in ("claude", "omp", "opencode"):
        assert name in msg


def test_unsupported_policy_exits_with_supported_list(env, capsys):
    env.setenv("POLICY", "bogus")
    msg = _refused(capsys)
    assert "supports policies" in msg
    for policy in ("default", "edits", "full", "readonly"):
        assert policy in msg


def test_missing_required_setting_exits_naming_it(env, capsys):
    env.delenv("MANAGER_URL")
    assert "manager_url" in _refused(capsys)


def test_unset_runtime_lists_available_runtimes(env, capsys):
    env.delenv("RUNTIME")
    msg = _refused(capsys)
    assert "runtime" in msg
    assert "available: claude, omp, opencode" in msg


def test_malformed_list_setting_exits_without_echoing_value(env, capsys):
    env.setenv("ENV_PASSTHROUGH", "SECRET-LOOKING-VALUE")
    msg = _refused(capsys)
    assert "env_passthrough" in msg
    assert "SECRET-LOOKING-VALUE" not in msg


def test_bad_runtime_args_exits_without_echoing_value(env, capsys):
    env.setenv("RUNTIME_ARGS", "--token=SECRET-LOOKING-VALUE")
    msg = _refused(capsys)
    assert "runtime_args" in msg
    assert "SECRET-LOOKING-VALUE" not in msg


def test_malformed_toml_exits_without_retry(env, tmp_path, capsys):
    cfg = tmp_path / "bad.toml"
    cfg.write_text('model = "SECRET-LOOKING-VALUE\n')
    env.setenv("ADAPTER_CONFIG", str(cfg))
    msg = _refused(capsys)
    assert "toml" in msg.lower()
    assert "SECRET-LOOKING-VALUE" not in msg


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
