"""Run card streaming + reply-context: fakes only, no live Discord."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord

from discord_frontend.config import Settings
from discord_frontend.bot import SessionBot, compose_prompt
from discord_frontend.ui.embeds import ago, display_title


class FakeTyping:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class FakeMessage:
    """A sent card: records embed edits and view swaps (no delete — the run
    card now flips to a terminal state instead)."""

    def __init__(self, embed=None, view=None):
        self.embed = embed
        self.view = view
        self.edits: list[dict] = []
        self.deleted = False

    async def edit(self, **kw) -> None:
        self.edits.append(kw)
        if "embed" in kw:
            self.embed = kw["embed"]
        if "view" in kw:
            self.view = kw["view"]

    async def delete(self) -> None:
        self.deleted = True


class FakeThread:
    def __init__(self):
        self.id = 777
        self.sent: list[str] = []
        self.messages: list[FakeMessage] = []

    def typing(self):
        return FakeTyping()

    async def send(self, text: str | None = None, embed=None, view=None,
                   **kw) -> FakeMessage:
        if text is not None:
            self.sent.append(text)
        msg = FakeMessage(embed=embed, view=view)
        self.messages.append(msg)
        return msg


class FakeHub:
    def __init__(self, rows=None, caps=None):
        self.rows = rows or {}
        self.caps = {"cancel"} if caps is None else caps

    async def recover_interrupted(self) -> list[str]:
        return list(self.rows)

    async def get_session(self, session_id: str):
        return self.rows[session_id]

    async def agents(self):
        caps = ", ".join(f'"{c}"' for c in sorted(self.caps))
        return [SimpleNamespace(id="omp", name="omp", status="online",
                                online=True, capabilities=f"[{caps}]")]

    async def events(self):
        return
        yield  # pragma: no cover — empty stream; setup_hook must not block


def srow(sid: str = "s1"):
    return SimpleNamespace(id=sid, title="T", workspace=None, agent_id="omp",
                           thread_id=123, status="running")


def make_bot(rows: dict | None = None, caps: set | None = None) -> SessionBot:
    # tiny stream_interval so a single sleep(0.05) spans several flush ticks
    return SessionBot(Settings(discord_owner_id=42, stream_interval=0.01),
                      FakeHub(rows or {}, caps))


def wire_thread(bot: SessionBot) -> FakeThread:
    thread = FakeThread()

    async def thread_for(session_id):
        return thread

    bot._thread_for = thread_for
    return thread


def test_compose_prompt_without_reply():
    assert compose_prompt("fix it", None, None) == "fix it"


async def test_run_card_edits_one_message_and_flips_to_terminal():
    bot = make_bot({"s1": srow()})
    thread = wire_thread(bot)

    bot._on_delta("s1", "part one")
    await asyncio.sleep(0.05)  # first flush creates the run card
    assert len(thread.messages) == 1
    card = thread.messages[0]
    assert card.embed.description.endswith("part one")
    assert card.embed.title.startswith("🟡")
    assert card.view is not None  # Stop button rendered (cancel capable)

    # further deltas edit the same message — one message per run, throttled
    bot._on_delta("s1", "part one and part two")
    await asyncio.sleep(0.05)
    assert len(thread.messages) == 1
    assert card.edits and card.edits[-1]["embed"].description.endswith(
        "part one and part two")

    await bot._finalize_run("s1", "completed")
    terminal = card.edits[-1]
    assert terminal["embed"].title.startswith("🟢")
    assert terminal["view"] is None  # controls stripped
    assert bot._live == {} and bot._flushers == {}


async def test_run_card_shows_activity_then_falls_back():
    bot = make_bot({"s1": srow()})
    thread = wire_thread(bot)

    bot._on_activity("s1", "bash: echo hi")
    bot._on_delta("s1", "partial output")
    await asyncio.sleep(0.05)
    card = thread.messages[0]
    field = card.embed.fields[0]
    assert field.name == "Activity" and "bash: echo hi" in field.value
    assert "🔧" in field.value

    bot._on_activity("s1", None)  # tool finished → generic working state
    await asyncio.sleep(0.05)
    assert "working…" in card.edits[-1]["embed"].fields[0].value
    await bot._finalize_run("s1", "completed")


async def test_run_card_without_cancel_capability_has_no_stop_button():
    bot = make_bot({"s1": srow()}, caps=set())
    thread = wire_thread(bot)

    bot._on_delta("s1", "partial")
    await asyncio.sleep(0.05)
    card = thread.messages[0]
    assert card.view is None  # do not expose unsupported controls
    await bot._finalize_run("s1", "cancelled")


async def test_event_completion_finalizes_run_card():
    bot = make_bot({"s1": srow()})
    thread = wire_thread(bot)

    bot._on_delta("s1", "partial")
    await asyncio.sleep(0.05)
    card = thread.messages[0]

    await bot._dispatch_event({"type": "session.completed",
                               "session_id": "s1"})
    assert card.edits and card.edits[-1]["view"] is None


def test_display_title_prefers_title_then_workspace():
    assert display_title(SimpleNamespace(title="T", workspace="/a/b",
                                         agent_id="x")) == "T"
    assert display_title(SimpleNamespace(title=None, workspace="/a/b",
                                         agent_id="x")) == "OMP · b"
    assert display_title(SimpleNamespace(title=None, workspace=None,
                                         agent_id="omp")) == "OMP · omp"


def test_ago_renders_relative_age():
    now = datetime.now(timezone.utc)
    assert ago(now) == "just now"
    assert ago(now - timedelta(minutes=5)) == "5m ago"
    assert ago(now - timedelta(hours=3)) == "3h ago"
    assert ago(now - timedelta(days=2)) == "2d ago"
    assert ago(None) == "?"


# --- sync rendering: missed printing / reprint / auto-thread ---------------


def mrow(mid, role, content, posted=None):
    from types import SimpleNamespace as NS
    return NS(id=mid, role=role, content=content, posted_at=posted,
              created_at=None)


class FakeHubWithMsgs(FakeHub):
    def __init__(self, rows, msgs, caps=None):
        super().__init__(rows, caps)
        self.msgs = msgs  # sid -> list[mrow]
        self.marked: list[int] = []
        self.attached: list[tuple[str, int]] = []

    async def messages(self, session_id, unposted=False):
        ms = self.msgs.get(session_id, [])
        if unposted:
            return [m for m in ms if m.posted_at is None]
        return ms

    async def message_count(self, session_id):
        return len(self.msgs.get(session_id, []))

    async def missed_session_ids(self):
        return [sid for sid, ms in self.msgs.items()
                if any(m.posted_at is None for m in ms)]

    async def mark_posted(self, session_id, ids):
        self.marked.extend(ids)

    async def attach_thread(self, session_id, thread_id):
        self.attached.append((session_id, thread_id))


def srow_full(sid="s1", thread_id=123):
    import datetime as dt
    from types import SimpleNamespace as NS
    return NS(id=sid, title="T", workspace=None, agent_id="omp",
              thread_id=thread_id, status="idle", archived=False,
              created_at=None, updated_at=dt.datetime(2026, 9, 18))


async def test_print_missed_posts_and_marks():
    sessions = FakeHubWithMsgs({"s1": srow_full()},
                               {"s1": [mrow(1, "user", "hello"),
                                       mrow(2, "assistant", "hi there")]})
    bot = SessionBot(Settings(discord_owner_id=42, stream_interval=0.01),
                     sessions)
    thread = wire_thread(bot)
    n = await bot.print_missed(sessions.rows["s1"])
    assert n == 2
    assert thread.sent[0].startswith("**You:**")
    assert thread.sent[1].startswith("**Agent:**")
    assert sessions.marked == [1, 2]


async def test_reprint_transcript_posts_all_with_header():
    sessions = FakeHubWithMsgs(
        {"s1": srow_full()},
        {"s1": [mrow(1, "user", "q", posted="x"),
                mrow(2, "assistant", "a", posted="x")]})
    bot = SessionBot(Settings(discord_owner_id=42, stream_interval=0.01),
                     sessions)
    thread = wire_thread(bot)
    n = await bot.reprint_transcript(sessions.rows["s1"])
    assert n == 2 and len(thread.sent) == 2
    assert sessions.marked == [1, 2]  # reprinted ⇒ counts as rendered


async def test_auto_thread_created_in_sync_channel():
    created = {}

    class FakeChannel:
        async def create_thread(self, name, type):
            t = FakeThread()
            created["thread"] = t
            return t

    async def fake_fetch(cid):
        created["channel_id"] = cid
        return FakeChannel()

    sessions = FakeHubWithMsgs({"s1": srow_full(thread_id=None)},
                               {"s1": [mrow(1, "user", "hello")]})
    bot = SessionBot(Settings(discord_owner_id=42, discord_sync_channel_id=999,
                              stream_interval=0.01),
                     sessions)
    bot.fetch_channel = fake_fetch
    thread = await bot._ensure_thread(sessions.rows["s1"])
    assert created["channel_id"] == 999
    assert sessions.attached == [("s1", thread.id)]


async def test_print_missed_gives_up_on_client_error():
    class Rejecting(FakeThread):
        async def send(self, text=None, **kw):
            err = discord.HTTPException(
                SimpleNamespace(status=403, reason="Forbidden"), "bad")
            raise err

    sessions = FakeHubWithMsgs({"s1": srow_full()},
                               {"s1": [mrow(1, "user", "hello")]})
    bot = SessionBot(Settings(discord_owner_id=42, stream_interval=0.01),
                     sessions)
    bot.get_channel = lambda cid: Rejecting()
    n = await bot.print_missed(sessions.rows["s1"])
    assert n == 0 and sessions.marked == [1]  # 4xx: marked, warned, no loop


async def test_reprint_warns_and_marks_on_give_up(caplog):
    class Rejecting(FakeThread):
        async def send(self, text=None, **kw):
            if text is not None:  # the header card goes through; text 4xx's
                raise discord.HTTPException(
                    SimpleNamespace(status=403, reason="Forbidden"), "bad")
            return await super().send(text=text, **kw)

    sessions = FakeHubWithMsgs(
        {"s1": srow_full()},
        {"s1": [mrow(1, "user", "q", posted="x"),
                mrow(2, "assistant", "a", posted="x")]})
    bot = SessionBot(Settings(discord_owner_id=42, stream_interval=0.01),
                     sessions)
    bot.get_channel = lambda cid: Rejecting()
    with caplog.at_level(logging.WARNING, logger="discord_frontend.bot"):
        n = await bot.reprint_transcript(sessions.rows["s1"])
    assert n == 2 and sessions.marked == [1, 2]  # still marked rendered
    assert "403" in caplog.text and "s1" in caplog.text  # warned per message


async def test_print_missed_serializes_concurrent_calls():
    class SlowThread(FakeThread):
        async def send(self, text=None, **kw):
            await asyncio.sleep(0.01)  # force overlap between the two calls
            return await super().send(text=text, **kw)

    class SelfMarking(FakeHubWithMsgs):
        async def mark_posted(self, session_id, ids):
            await super().mark_posted(session_id, ids)
            for ms in self.msgs.values():
                for m in ms:
                    if m.id in ids:
                        m.posted_at = "x"

    sessions = SelfMarking({"s1": srow_full()},
                           {"s1": [mrow(1, "user", "hello")]})
    bot = SessionBot(Settings(discord_owner_id=42, stream_interval=0.01),
                     sessions)
    thread = SlowThread()

    async def thread_for(session_id):
        return thread

    bot._thread_for = thread_for
    row = sessions.rows["s1"]
    await asyncio.gather(bot.print_missed(row), bot.print_missed(row))
    assert thread.sent == ["**You:**\nhello"]  # printed once, not twice
    assert sessions.marked == [1]  # marked once


async def test_on_sync_applied_tracks_and_releases_task():
    sessions = FakeHubWithMsgs({"s1": srow_full()},
                               {"s1": [mrow(1, "user", "hello")]})
    bot = SessionBot(Settings(discord_owner_id=42, stream_interval=0.01),
                     sessions)
    wire_thread(bot)
    await bot.on_sync_applied("omp", ["s1"])  # async hook: awaited cleanly
    assert bot._sync_tasks  # held while in flight, not GC-able
    await asyncio.sleep(0.6)  # pacing sleep for 1 message
    assert sessions.marked == [1]
    assert not bot._sync_tasks  # done callback released the reference


async def test_print_missed_warns_on_give_up(caplog):
    class Rejecting(FakeThread):
        async def send(self, text=None, **kw):
            raise discord.HTTPException(
                SimpleNamespace(status=403, reason="Forbidden"), "bad")

    sessions = FakeHubWithMsgs({"s1": srow_full()},
                               {"s1": [mrow(1, "user", "hello")]})
    bot = SessionBot(Settings(discord_owner_id=42, stream_interval=0.01),
                     sessions)
    bot.get_channel = lambda cid: Rejecting()
    with caplog.at_level(logging.WARNING, logger="discord_frontend.bot"):
        n = await bot.print_missed(sessions.rows["s1"])
    assert n == 0 and sessions.marked == [1]
    assert "403" in caplog.text and "s1" in caplog.text


# --- /sync: nudge the hub, print missed everywhere -------------------------


def StaticAsync(fn):
    async def call(*a, **kw):
        return fn(*a, **kw)
    return call


async def test_cmd_sync_nudges_and_summarizes():
    sent = []

    sessions = FakeHubWithMsgs({}, {})
    sessions.nudged = 1

    async def fake_nudge():
        return sessions.nudged

    sessions.sync_nudge = fake_nudge
    bot = SessionBot(Settings(discord_owner_id=42, stream_interval=0.01),
                     sessions)

    async def fake_everywhere(deadline=8.0):
        return (3, 2)

    bot.print_missed_everywhere = fake_everywhere

    class FakeInteraction:
        user = SimpleNamespace(id=42)
        response = SimpleNamespace(
            defer=StaticAsync(lambda **kw: sent.append(("defer", kw))))
        followup = SimpleNamespace(
            send=StaticAsync(lambda text, **kw: sent.append(
                ("reply", text))))

    await bot._cmd_sync(FakeInteraction())
    assert ("defer", {"ephemeral": True, "thinking": True}) in sent
    assert any(r[0] == "reply" and "nudged 1 agent" in r[1]
               for r in sent if r[0] == "reply")
    assert any(r[0] == "reply" and "3" in r[1] and "2" in r[1]
               for r in sent if r[0] == "reply")


async def test_cmd_open_reports_failed_thread_create():
    sent = []

    class FakeChannel:
        async def create_thread(self, name, type):
            raise RuntimeError("boom")

    class FakeInteraction:
        user = SimpleNamespace(id=42)
        guild_id = 1
        channel = FakeChannel()
        response = SimpleNamespace(
            defer=StaticAsync(lambda **kw: sent.append(("defer", kw))))
        followup = SimpleNamespace(
            send=StaticAsync(lambda text, **kw: sent.append(
                ("reply", text))))

    sessions = FakeHubWithMsgs({"s1": srow_full()}, {"s1": []})
    bot = SessionBot(Settings(discord_owner_id=42, stream_interval=0.01),
                     sessions)
    await bot._cmd_open(FakeInteraction(), "s1")
    assert ("defer", {"thinking": True}) in sent
    assert any(r[0] == "reply" and r[1].startswith("session open failed:")
               and "boom" in r[1] for r in sent)
