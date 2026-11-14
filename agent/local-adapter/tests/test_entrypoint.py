"""The daemon supervisor must survive crashes and keep the agent online."""
import asyncio

import pytest

import agent_adapter.__main__ as entry


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
