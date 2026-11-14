"""omp driver: `omp --mode=json` (NDJSON events). Behavior moved verbatim
from the former omp.py / SessionRuntime._forward_stream_event."""
from __future__ import annotations

import logging
import pathlib
import signal
import sqlite3

from agent_adapter.runtimes import RuntimeSpec
from agent_adapter.runtimes.base import (Activity, Delta, JsonlProcessRunner,
                                         Parsed, RunRequest)

# policy -> argv flags. `default` = omp's own configuration (today's
# behavior). Other entries were verified against the real omp 18.1.19 with
# `omp --mode=json --approval-mode=<m> -p 'Create made.txt containing ok, then
# run: echo hi'` (headless, no UI to approve):
#   --approval-mode=always-ask  write DENIED, bash DENIED, exit 0, no hang
#   --approval-mode=write       write DONE,   bash DENIED, exit 0, no hang
#   --approval-mode=yolo        write DONE,   bash DONE,   exit 0, no hang
# An unlisted policy refuses to start.
POLICIES: dict[str, tuple[str, ...]] = {
    "default": (),
    "readonly": ("--approval-mode=always-ask",),
    "edits": ("--approval-mode=write",),
    "full": ("--approval-mode=yolo",),
}


class OmpParser:
    def feed(self, ev: dict) -> Parsed:
        p = Parsed()
        t = ev.get("type")
        if t == "session":
            p.session_id = ev.get("id")
        elif t == "message_end" and (
                (ev.get("message") or {}).get("role") == "assistant"):
            p.final_text = "".join(
                c.get("text", "") for c in ev["message"].get("content", [])
                if c.get("type") == "text")
        elif t == "message_update":
            a = ev.get("assistantMessageEvent") or {}
            at = a.get("type", "")
            if at == "text_start":
                p.events.append(Delta(reset=True))
            elif at == "text_delta":
                p.events.append(Delta(text=a.get("delta", "")))
            elif at == "thinking_start":
                p.events.append(Activity("think"))
        elif t == "tool_execution_start":
            args = ev.get("args") or {}
            detail = (ev.get("intent")
                      or " ".join(str(v) for v in args.values() if v))[:120]
            p.events.append(Activity("tool", "start",
                                     ev.get("toolName", "tool"), detail))
        elif t == "tool_execution_end":
            p.events.append(Activity("tool", "end",
                                     ev.get("toolName", "tool")))
        return p


class OmpDriver:
    name = "omp"
    require_final_text = False   # omp exit 0 without a reply yields ""
    cancel_signal = signal.SIGTERM
    auth_env = ("*_API_KEY",)    # omp reads provider keys from env or its store

    def __init__(self, settings) -> None:
        self.settings = settings

    def argv(self, binary: str, req: RunRequest) -> list[str]:
        args = [binary, "--mode=json", "--cwd", req.workspace]
        if req.resume_id:
            args += ["-r", req.resume_id]
        args += list(POLICIES[req.policy])
        if req.model:
            args += ["--model", req.model]
        args += list(self.settings.runtime_args)
        # `-p` is a boolean flag; the prompt is a positional MESSAGES arg.
        # `--` ends option parsing so hub text such as "--approval-mode=yolo"
        # or "--version" can never be read as an omp flag (verified on omp
        # 18.1.19: without `--`, `-p --version` prints the version and exits).
        args += ["-p", "--", req.text]
        return args

    def parser(self) -> OmpParser:
        return OmpParser()


logger = logging.getLogger(__name__)


def omp_titles(settings, native_ids: list[str]) -> dict[str, str]:
    """Human session titles from omp's own store, read-only. Moved from
    SessionRuntime.refresh_titles; the journaling loop stays in
    SessionRuntime. Missing DB or no ids -> {} (omp titles lazily, so
    absence is normal); a sqlite error logs a warning and returns what was
    read so far."""
    db_path = pathlib.Path(settings.omp_titles_db).expanduser()
    if not native_ids or not db_path.exists():
        return {}
    out: dict[str, str] = {}
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            for native in native_ids:
                row = con.execute(
                    "SELECT title FROM session_titles WHERE session_id=?",
                    (native,)).fetchone()
                title = str(row[0]).strip() if row and row[0] else ""
                if title:
                    out[native] = title
        finally:
            con.close()
    except sqlite3.Error:
        logger.warning("omp titles db unreadable (%s); skipping",
                       db_path, exc_info=True)
    return out


SPEC = RuntimeSpec(
    name="omp", streaming=True, policies=frozenset(POLICIES),
    make_runner=lambda settings: JsonlProcessRunner(OmpDriver(settings),
                                                    settings),
    titles=omp_titles)
