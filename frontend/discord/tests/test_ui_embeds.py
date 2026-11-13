"""UI builder tests: icons, embed shapes, capability checklists — no Discord
connection, embeds are plain objects."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from discord_frontend.ui import embeds, icons


def row(**kw):
    base = dict(id="sess_1", agent_id="omp", owner_id="42", thread_id=555,
                status="completed", workspace=None, title="OCR Project",
                archived=False, omp_session_id=None,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                updated_at=datetime.now(timezone.utc).replace(tzinfo=None))
    return SimpleNamespace(**{**base, **kw})


def test_status_icons_and_colors_cover_all_statuses():
    for status in ("created", "waiting", "running", "completed", "failed",
                   "cancelled"):
        assert status in icons.STATUS_ICON
        assert status in icons.STATUS_COLOR
    assert icons.status_icon("nonsense") == "⚪"  # unknown falls back


def test_activity_icon_mapping():
    assert icons.activity_icon(None) == "⏳"
    assert icons.activity_icon("thinking") == "🧠"
    assert icons.activity_icon("grep: src") == "🔍"
    assert icons.activity_icon("read: a.py") == "📄"
    assert icons.activity_icon("bash: ls") == "🔧"
    assert icons.activity_icon("pytest: tests") == "⏳"
    assert icons.activity_icon("mystery_tool: x") == "🔧"  # fallback


def test_draft_tail_caps_at_limit():
    assert embeds.draft_tail("hello") == "hello"
    long = "x" * (embeds.LIVE_DRAFT_LIMIT + 100)
    tail = embeds.draft_tail(long)
    assert tail.startswith("…")
    assert len(tail) == embeds.LIVE_DRAFT_LIMIT + 1


def test_fmt_duration():
    assert embeds.fmt_duration(42) == "42s"
    assert embeds.fmt_duration(95) == "1m 35s"
    assert embeds.fmt_duration(3720) == "1h 2m"


def test_run_card_running_shape():
    e = embeds.run_card(title="OCR", agent_id="omp", status="running",
                        draft="partial answer", activity="bash: ls",
                        started_at=datetime.now(timezone.utc))
    assert e.title.startswith("🟡")
    assert "Running" in e.title and "OCR" in e.title
    assert e.description == "partial answer"
    act = e.fields[0]
    assert "🔧" in act.value and "bash: ls" in act.value
    assert e.footer.text == "Agent Hub · omp"
    assert e.timestamp is not None


def test_run_card_terminal_states():
    failed = embeds.run_card(title="OCR", agent_id="omp", status="failed",
                             reason="boom", elapsed=95)
    assert failed.title.startswith("🔴") and "Failed" in failed.title
    assert any("boom" in f.value for f in failed.fields)
    assert any("1m 35s" in f.value for f in failed.fields)
    cancelled = embeds.run_card(title="OCR", agent_id="omp",
                                status="cancelled")
    assert cancelled.title.startswith("🟣")
    done = embeds.run_card(title="OCR", agent_id="omp", status="completed")
    assert done.title.startswith("🟢") and "Completed" in done.title


def test_run_card_empty_draft_placeholder():
    e = embeds.run_card(title="OCR", agent_id="omp", status="running")
    assert "no output" in e.description


def test_session_card_fields():
    r = row(workspace="~/proj/ocr")
    e = embeds.session_card(r, online=True, messages=47)
    assert e.title.startswith("🟢") and "OCR Project" in e.title
    joined = {f.name: f.value for f in e.fields}
    assert "🟢 online" in joined["Agent"]
    assert "47" in joined["Messages"]
    assert "~/proj/ocr" in joined["Workspace"]
    assert e.footer.text == "Agent Hub · sess_1"


def test_session_card_archived_flag():
    e = embeds.session_card(row(status="created", archived=True),
                            online=False, messages=0)
    assert "⚫" in e.title
    joined = {f.name: f.value for f in e.fields}
    assert "archived" in joined["Status"]
    assert "offline" in joined["Agent"]


def test_session_list_embed_lines():
    e = embeds.session_list_embed([row(status="running"),
                                   row(id="s2", archived=True, title="Old")],
                                  page=0, pages=1)
    assert "🟡" in e.description and "OCR Project" in e.description
    assert "<#555>" in e.description
    assert "⚫" in e.description and "Old" in e.description
    assert "1/1" in e.footer.text


def test_jump_card_with_and_without_thread():
    e = embeds.jump_card(row())
    assert "OCR Project" in e.title and "▶️" in e.title
    assert "<#555>" in e.description
    e2 = embeds.jump_card(row(thread_id=None))
    assert "no thread" in e2.description


def test_agent_status_capability_checklist():
    agents = [SimpleNamespace(id="omp", status="online",
                              capabilities='["chat","cancel","streaming"]'),
              SimpleNamespace(id="other", status="offline",
                              capabilities='not json')]
    e = embeds.agent_status_embed(agents)
    assert len(e.fields) == 2
    omp = e.fields[0].value
    assert "✓ chat" in omp and "✓ cancel" in omp
    assert "✗ interrupt" in omp and "✗ inject_input" in omp
    # malformed capability JSON degrades to empty checklist, not a crash
    assert "✗ interrupt" in e.fields[1].value


def test_help_embed_lists_commands():
    e = embeds.help_embed()
    for name in ("/session new", "/session list", "/session info",
                 "/session rename", "/session archive", "/session delete",
                 "/resume", "/run status", "/run cancel", "/agent status",
                 "/help"):
        assert name in e.description, f"{name} missing from help embed"


def test_parse_caps():
    assert embeds.parse_caps('["a","b"]') == ["a", "b"]
    assert embeds.parse_caps("junk") == []
    assert embeds.parse_caps(None) == []
