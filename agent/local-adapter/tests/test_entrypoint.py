"""The daemon supervisor must survive crashes and keep the agent online."""
import asyncio

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
