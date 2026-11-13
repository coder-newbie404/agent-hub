"""Embed builders — pure functions from rows/state to discord.Embed so every
surface shares one visual language. No I/O here."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import discord

from discord_frontend.ui.icons import (AGENT_STATUS_ICON, ARCHIVED_ICON, NOTABLE_CAPS,
                          RESUME, STATUS_COLOR, activity_icon, status_icon)

NEUTRAL = 0x9E9E9E
LIVE_DRAFT_LIMIT = 1500  # live card shows the draft tail only

_STATUS_WORD = {"created": "Running", "waiting": "Running", "running": "Running",
                "completed": "Completed", "failed": "Failed",
                "cancelled": "Cancelled"}


def ago(naive_utc: datetime | None) -> str:
    """Relative age of a naive-UTC datetime, for compact rendering."""
    if naive_utc is None:
        return "?"
    secs = max(0, int((datetime.now(timezone.utc)
                       - naive_utc.replace(tzinfo=timezone.utc)).total_seconds()))
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def display_title(row) -> str:
    """Thread-name-style title; /session new seeds row.title explicitly."""
    if row.title:
        return row.title
    if row.workspace:
        return f"OMP · {os.path.basename(row.workspace)}"
    return f"OMP · {row.agent_id}"


def parse_caps(raw: str | None) -> list[str]:
    try:
        return [str(c) for c in json.loads(raw or "[]")]
    except (TypeError, ValueError):
        return []


def draft_tail(text: str, limit: int = LIVE_DRAFT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


def fmt_duration(secs: float) -> str:
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    return f"{secs // 3600}h {secs % 3600 // 60}m"


def run_card(*, title: str, agent_id: str, status: str, draft: str = "",
             activity: str | None = None, started_at: datetime | None = None,
             elapsed: float | None = None, reason: str | None = None
             ) -> discord.Embed:
    """The in-thread run card — live while streaming, terminal afterwards."""
    icon = status_icon(status)
    e = discord.Embed(
        title=f"{icon} {_STATUS_WORD.get(status, status)} — {title}",
        description=draft_tail(draft) if draft else "*(no output streamed)*",
        color=STATUS_COLOR.get(status, NEUTRAL))
    e.add_field(name="Activity",
                value=f"{activity_icon(activity)} {activity or 'working…'}",
                inline=False)
    if elapsed is not None:
        e.add_field(name="Duration", value=f"⏱ {fmt_duration(elapsed)}")
    if reason:
        e.add_field(name="Reason", value=reason[:1000])
    e.set_footer(text=f"Agent Hub · {agent_id}")
    if started_at is not None:
        e.timestamp = started_at
    return e


def session_card(row, *, online: bool, messages: int) -> discord.Embed:
    flag = f" {ARCHIVED_ICON}" if row.archived else ""
    e = discord.Embed(title=f"{status_icon(row.status)} {display_title(row)}{flag}",
                      color=STATUS_COLOR.get(row.status, NEUTRAL))
    e.add_field(name="Agent",
                value=f"{row.agent_id} · "
                      f"{'🟢 online' if online else '⚪ offline'}")
    e.add_field(name="Status",
                value=row.status + (f" · {ARCHIVED_ICON} archived"
                                    if row.archived else ""))
    e.add_field(name="Messages", value=str(messages))
    if row.workspace:
        e.add_field(name="Workspace", value=f"`{row.workspace}`", inline=False)
    e.add_field(name="Created · Active",
                value=f"{ago(row.created_at)} · {ago(row.updated_at)}")
    e.set_footer(text=f"Agent Hub · {row.id}")
    return e


def session_list_embed(rows, *, page: int, pages: int) -> discord.Embed:
    lines = []
    for r in rows:
        mark = f" {ARCHIVED_ICON}" if r.archived else ""
        link = f" → <#{r.thread_id}>" if r.thread_id else ""
        lines.append(f"{status_icon(r.status)}{mark} **{display_title(r)}**"
                     f" — {r.status} · {ago(r.updated_at)}{link}")
    e = discord.Embed(title="📋 Sessions",
                      description="\n".join(lines) or "*(none)*",
                      color=NEUTRAL)
    e.set_footer(text=f"page {page + 1}/{pages}")
    return e


def jump_card(row) -> discord.Embed:
    dest = f"→ <#{row.thread_id}>" if row.thread_id else "→ *(no thread)*"
    e = discord.Embed(title=f"{RESUME} {display_title(row)}", description=dest,
                      color=NEUTRAL)
    e.set_footer(text=f"Agent Hub · {row.id}")
    return e


def missing_thread_card(row) -> discord.Embed:
    """Jump card for a session whose thread was deleted: no dead link, a
    fresh thread can be attached instead (transcript/resume survive)."""
    e = discord.Embed(
        title=f"{RESUME} {display_title(row)}",
        description="→ *(thread was deleted — press New thread to continue "
                    "this session in a fresh one)*",
        color=NEUTRAL)
    e.set_footer(text=f"Agent Hub · {row.id}")
    return e


def agent_status_embed(agents) -> discord.Embed:
    e = discord.Embed(title="🤖 Agents", color=NEUTRAL)
    for r in agents:
        dot = AGENT_STATUS_ICON.get(r.status, "⚪")
        declared = parse_caps(r.capabilities)
        lines = [f"✓ {c}" for c in declared]
        lines += [f"✗ {c}" for c in NOTABLE_CAPS if c not in declared]
        e.add_field(name=f"{dot} {r.id} · {r.status}",
                    value="\n".join(lines) or "*(none declared)*",
                    inline=False)
    return e


def help_embed() -> discord.Embed:
    e = discord.Embed(
        title="🤖 Agent Hub",
        description="\n".join([
            "**Agent Hub commands**",
            "`/session new [title] [workspace] [agent]` — start a session; creates a thread; agent autocompletes online agents",
            "`/session list` — session picker (recent first)",
            "`/session info [session_id]` — details (defaults to this thread's session)",
            "`/session rename <title>` — rename this thread's session and its thread",
            "`/session archive` — archive this thread's session",
            "`/session delete [session_id]` — delete a session and its transcript (thread kept)",
            "`/session open <session_id>` — open an old session in a new thread (reprints the transcript)",
            "`/resume` — pick a recent session to continue",
            "`/run status [session_id]` — run state + current activity",
            "`/run cancel` — stop the in-flight run in this thread",
            "`/agent status` — agents, online/offline, capabilities",
            "`/agent token new <agent_id>` — mint a per-agent auth token (shown once)",
            "`/agent token rotate <agent_id>` — revoke active tokens, mint a replacement",
            "`/agent token revoke <agent_id>` — revoke an agent's tokens",
            "`/agent token list` — tokens and their state",
            "`/sync` — nudge agents, then print missed messages in all threads",
            "`/help` — this list",
            "",
            "Typing in a session thread sends a message to the agent.",
            "Replying to an earlier message quotes it as context for the agent.",
            "While a run is live, the run card shows activity, streamed text"
            " and a 🛑 Stop button when the agent supports it.",
        ]),
        color=NEUTRAL)
    return e
