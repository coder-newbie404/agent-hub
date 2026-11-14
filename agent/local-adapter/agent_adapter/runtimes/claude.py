"""Claude Code driver: `claude -p ... --output-format stream-json`.
Docs-derived (https://code.claude.com/docs/en/headless.md): verified only
against fixtures until run against a real binary -> EXPERIMENTAL."""
from __future__ import annotations

import signal

from agent_adapter.runtimes import RuntimeSpec
from agent_adapter.runtimes.base import (Activity, Delta, JsonlProcessRunner,
                                         Parsed, RunRequest)

POLICIES = {
    "default": (),
    "readonly": ("--permission-mode", "plan"),
    "edits": ("--permission-mode", "acceptEdits"),
    "full": ("--permission-mode", "bypassPermissions"),
}


class ClaudeParser:
    def __init__(self) -> None:
        self._tools: dict[str, str] = {}   # tool_use id -> name

    def feed(self, obj: dict) -> Parsed:
        p = Parsed(session_id=obj.get("session_id"))
        t = obj.get("type")
        if t == "stream_event":
            ev = obj.get("event") or {}
            if ev.get("type") == "content_block_start" and (
                    (ev.get("content_block") or {}).get("type") == "text"):
                p.events.append(Delta(reset=True))
            else:
                d = ev.get("delta") or {}
                if d.get("type") == "text_delta":
                    p.events.append(Delta(text=d.get("text", "")))
        elif t == "assistant":
            for block in (obj.get("message") or {}).get("content", []):
                if block.get("type") == "tool_use":
                    name = block.get("name", "tool")
                    self._tools[block.get("id", "")] = name
                    args = block.get("input") or {}
                    detail = " ".join(str(v) for v in args.values() if v)[:120]
                    p.events.append(Activity("tool", "start", name, detail))
        elif t == "user":
            for block in (obj.get("message") or {}).get("content", []):
                if block.get("type") == "tool_result":
                    name = self._tools.get(block.get("tool_use_id", ""), "tool")
                    p.events.append(Activity("tool", "end", name))
        elif t == "result":
            p.final_text = obj.get("result") or ""
            if obj.get("is_error"):
                p.error = str(obj.get("result") or obj.get("subtype")
                              or "claude error")
        return p


class ClaudeDriver:
    name = "claude"
    require_final_text = True
    cancel_signal = signal.SIGINT   # SIGTERM drops the in-progress turn
    auth_env = ("ANTHROPIC_*", "CLAUDE_CODE_*", "CLAUDE_*", "AWS_*",
                "GOOGLE_APPLICATION_CREDENTIALS", "CLOUD_ML_REGION",
                "ANTHROPIC_VERTEX_PROJECT_ID")

    def __init__(self, settings) -> None:
        self.settings = settings

    def argv(self, binary: str, req: RunRequest) -> list[str]:
        args = [binary, "-p", req.text, "--output-format", "stream-json",
                "--verbose", "--include-partial-messages"]
        if req.resume_id:
            args += ["--resume", req.resume_id]
        args += list(POLICIES[req.policy])
        if req.model:
            args += ["--model", req.model]
        args += list(self.settings.runtime_args)
        return args            # cwd is the workspace (no --cwd flag)

    def parser(self) -> ClaudeParser:
        return ClaudeParser()


SPEC = RuntimeSpec(
    name="claude", streaming=True, policies=frozenset(POLICIES),
    make_runner=lambda settings: JsonlProcessRunner(ClaudeDriver(settings),
                                                    settings))
