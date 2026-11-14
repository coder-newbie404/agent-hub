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


# Tool-input keys safe to surface as an activity detail, by priority. Never
# join arbitrary values: Write/Edit inputs carry whole file contents.
_DETAIL_KEYS = ("command", "file_path", "path", "pattern", "query", "url",
                "description", "prompt")
_DETAIL_MAX = 120


def _dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _blocks(message) -> list[dict]:
    """Dict content blocks of a message; any other shape yields nothing."""
    content = _dict(message).get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


def _detail(args) -> str:
    args = _dict(args)
    for key in _DETAIL_KEYS:
        v = args.get(key)
        if isinstance(v, str) and v:
            return v[:_DETAIL_MAX]
    return ""


def _str(v, default: str = "") -> str:
    return v if isinstance(v, str) else default


class ClaudeParser:
    """Total over arbitrary JSON objects: unexpected shapes are ignored (an
    exception here would make the runner SIGKILL an otherwise fine run)."""

    def __init__(self) -> None:
        self._tools: dict[str, str] = {}   # tool_use id -> name

    def feed(self, obj: dict) -> Parsed:
        sid = obj.get("session_id")
        p = Parsed(session_id=sid if isinstance(sid, str) else None)
        t = obj.get("type")
        if t == "stream_event":
            ev = _dict(obj.get("event"))
            if ev.get("type") == "content_block_start" and (
                    _dict(ev.get("content_block")).get("type") == "text"):
                p.events.append(Delta(reset=True))
            else:
                d = _dict(ev.get("delta"))
                text = d.get("text")
                if d.get("type") == "text_delta" and isinstance(text, str):
                    p.events.append(Delta(text=text))
        elif t == "assistant":
            for block in _blocks(obj.get("message")):
                if block.get("type") == "tool_use":
                    name = _str(block.get("name"), "tool")
                    self._tools[_str(block.get("id"))] = name
                    p.events.append(Activity(
                        "tool", "start", name, _detail(block.get("input"))))
        elif t == "user":
            for block in _blocks(obj.get("message")):
                if block.get("type") == "tool_result":
                    name = self._tools.get(_str(block.get("tool_use_id")),
                                           "tool")
                    p.events.append(Activity("tool", "end", name))
        elif t == "result":
            result = _str(obj.get("result"))
            p.final_text = result
            if obj.get("is_error") is True:
                p.error = (result or _str(obj.get("subtype"))
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
        args = [binary, "-p", "--output-format", "stream-json", "--verbose",
                "--include-partial-messages"]
        if req.resume_id:
            args += ["--resume", req.resume_id]
        args += list(POLICIES[req.policy])
        if req.model:
            args += ["--model", req.model]
        args += list(self.settings.runtime_args)
        # Prompt text is untrusted (hub message) and goes LAST after `--`, so
        # a message like `--dangerously-skip-permissions` cannot be parsed as a
        # flag. UNVERIFIED against a real claude binary (none installed here):
        # derived from the docs' "-p/--print is boolean, prompt is positional"
        # and POSIX `--` semantics.
        args += ["--", req.text]
        return args            # cwd is the workspace (no --cwd flag)

    def parser(self) -> ClaudeParser:
        return ClaudeParser()


SPEC = RuntimeSpec(
    name="claude", streaming=True, policies=frozenset(POLICIES),
    make_runner=lambda settings: JsonlProcessRunner(ClaudeDriver(settings),
                                                    settings))
