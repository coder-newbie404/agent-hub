"""Smoke test against the REAL agent CLIs. Calls live LLMs, so it is skipped
unless RUN_REAL_BINARIES=1 and the binary is on PATH; the default suite never
runs it.

    RUN_REAL_BINARIES=1 .venv/bin/python -m pytest tests/test_real_binaries.py -v

opencode uses REAL_OPENCODE_MODEL (default: the free
opencode/glm-5.3-flash-free). Each runtime does two calls: a first turn, then
a resume turn that must keep the same native session id.
"""
import asyncio
import os
import shutil

import pytest

from agent_adapter.config import Settings
from agent_adapter.runtimes import get_runtime
from agent_adapter.runtimes.base import RunRequest

PROMPT = "Reply with exactly the single word: pong"
TIMEOUT = 120

enabled = pytest.mark.skipif(
    os.environ.get("RUN_REAL_BINARIES") != "1",
    reason="live LLM calls: set RUN_REAL_BINARIES=1")


def real_case(name: str, model: str | None = None):
    return pytest.param(
        name, model, id=name,
        marks=[enabled, pytest.mark.skipif(
            shutil.which(name) is None, reason=f"{name} not on PATH")])


@pytest.mark.parametrize("name,model", [
    real_case("omp"),
    real_case("opencode",
              os.environ.get("REAL_OPENCODE_MODEL",
                             "opencode/glm-5.3-flash-free")),
])
async def test_real_binary_runs_and_resumes(tmp_path, name, model):
    settings = Settings(manager_url="wss://x", agent_token="t",
                        default_workspace=str(tmp_path), runtime=name,
                        model=model, journal_dir=str(tmp_path / "journal"),
                        delegation_dir=str(tmp_path / "delegations"))
    runner = get_runtime(name).make_runner(settings)

    async def noop(event):
        pass

    async def turn(resume_id):
        return await asyncio.wait_for(
            runner.run(RunRequest(workspace=str(tmp_path),
                                  resume_id=resume_id, text=PROMPT,
                                  model=model), noop),
            TIMEOUT)

    first = await turn(None)
    assert first.resume_id
    assert "pong" in first.text.lower()
    second = await turn(first.resume_id)
    assert second.resume_id == first.resume_id
    assert "pong" in second.text.lower()
