"""opencode driver: `opencode run <text> --format json`. Schema and error
behavior verified against opencode 1.18.29 (see tests/fixtures/opencode)."""
from __future__ import annotations

import signal

from agent_adapter.runtimes import RuntimeSpec
from agent_adapter.runtimes.base import (Activity, Delta, JsonlProcessRunner,
                                         Parsed, RunRequest)

# opencode's headless `run` can only be switched fully open: an in-workspace
# write is allowed by default (config decides), an external write is
# auto-rejected, and `--auto` is the only flag. readonly/edits cannot be
# enforced, so they are unsupported (refuse to start).
POLICIES: dict[str, tuple[str, ...]] = {"default": (), "full": ("--auto",)}

# Verified live against opencode 1.18.29 with `run 'x' -m nope/nope --format
# json`: stdout is one generic JSON `error` event ("Unexpected server error")
# in every combination and stays valid JSONL; the real cause
# (`ProviderModelNotFoundError: Model not found: nope/nope`) reaches stderr
# only with --print-logs. --log-level WARN and ERROR both reduce that to the
# two ERROR lines carrying the cause (INFO/default add ~15 lines of config
# loading noise; --log-level alone prints nothing). ERROR is the lowest
# verbosity that still shows it.
ERROR_VISIBILITY_FLAGS = ("--print-logs", "--log-level", "ERROR")


# Tool input keys that describe WHAT a tool acts on, in priority order. Other
# inputs (notably file `content`, edit bodies) are never forwarded: the detail
# is sent to the hub and shown in chat.
DETAIL_KEYS = ("command", "filePath", "path", "pattern", "query", "url",
               "description")


def _tool_detail(inp: dict) -> str:
    for key in DETAIL_KEYS:
        value = inp.get(key)
        if value:
            return " ".join(str(value).split())[:120]
    return ""


class OpencodeParser:
    def feed(self, obj: dict) -> Parsed:
        p = Parsed(session_id=obj.get("sessionID"))
        t = obj.get("type")
        part = obj.get("part") or {}
        if t == "text":
            text = part.get("text", "")
            p.events.append(Delta(reset=True))
            p.events.append(Delta(text=text))
            if text:                       # the last non-empty text part wins
                p.final_text = text
        elif t == "tool_use":
            # Emitted once, already finished (completed or error): there are
            # no separate start/end events, so emit both.
            tool = part.get("tool", "tool")
            inp = (part.get("state") or {}).get("input") or {}
            p.events.append(Activity("tool", "start", tool, _tool_detail(inp)))
            p.events.append(Activity("tool", "end", tool))
        elif t == "error":
            data = (obj.get("error") or {}).get("data") or {}
            p.error = str(data.get("message")
                          or (obj.get("error") or {}).get("name")
                          or "opencode error")
        return p


class OpencodeDriver:
    name = "opencode"
    # A rejected permission ends the run with exit 0 and no final text.
    require_final_text = True
    cancel_signal = signal.SIGTERM
    auth_env = ("*_API_KEY", "OPENCODE_*")

    def __init__(self, settings) -> None:
        self.settings = settings

    def argv(self, binary: str, req: RunRequest) -> list[str]:
        args = [binary, "run", "--format", "json"]
        args += ERROR_VISIBILITY_FLAGS
        if req.resume_id:
            args += ["-s", req.resume_id]
        args += ["--dir", req.workspace]
        args += POLICIES[req.policy]
        if req.model:
            args += ["-m", req.model]
        args += list(self.settings.runtime_args)
        # yargs parses a leading-dash positional as a flag (`--auto ...` would
        # flip auto-approve, `- x` prints usage and exits 0): flags first, then
        # `--`, then the untrusted text last.
        args += ["--", req.text]
        return args

    def parser(self) -> OpencodeParser:
        return OpencodeParser()


SPEC = RuntimeSpec(
    name="opencode", streaming=False, policies=frozenset(POLICIES),
    make_runner=lambda settings: JsonlProcessRunner(OpencodeDriver(settings),
                                                    settings))
