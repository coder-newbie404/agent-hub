"""Bot lifecycle hardening: typing-hold bookkeeping (I1) and restart
recovery resilience (I2) — exercised with fakes, no live Discord."""
import asyncio
from types import SimpleNamespace

from discord_frontend.config import Settings
from discord_frontend.bot import SessionBot

RECOVERY_NOTICE = "manager restarted; the in-flight run was lost"


class FakeTyping:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class FakeThread:
    def __init__(self):
        self.sent: list[str] = []

    def typing(self):
        return FakeTyping()

    async def send(self, text: str) -> None:
        self.sent.append(text)


class FakeHub:
    def __init__(self, rows: dict):
        self.rows = rows

    async def recover_interrupted(self) -> list[str]:
        return list(self.rows)

    async def get_session(self, session_id: str):
        return self.rows[session_id]

    async def events(self):
        return
        yield  # pragma: no cover — empty stream; setup_hook must not block


def make_bot(rows: dict) -> SessionBot:
    return SessionBot(Settings(discord_owner_id=42), FakeHub(rows))


async def test_start_typing_cancels_displaced_hold():
    bot = make_bot({})

    async def thread_for(session_id):
        return FakeThread()

    bot._thread_for = thread_for

    await bot._start_typing("s1")
    first = bot._typing["s1"]
    assert not first.done()

    # a second message starts a new hold; the displaced one must be
    # cancelled, not overwritten-and-leaked
    await bot._start_typing("s1")
    for _ in range(3):
        await asyncio.sleep(0)

    assert first.cancelled()
    assert len(bot._typing) == 1

    await bot._finish_request("s1")
    assert bot._typing == {}


async def test_setup_hook_syncs_commands_and_survives_deleted_thread(monkeypatch):
    ok = FakeThread()
    rows = {"s_dead": SimpleNamespace(thread_id=111),
            "s_ok": SimpleNamespace(thread_id=222)}
    bot = make_bot(rows)
    events: list = []

    async def fake_sync(guild=None):
        events.append("sync")

    async def thread_row(row):
        if row.thread_id == 111:
            raise RuntimeError("thread deleted")
        return ok

    monkeypatch.setattr(bot.tree, "sync", fake_sync)
    monkeypatch.setattr(bot, "_thread_row", thread_row)

    await bot.setup_hook()

    # command sync happened even though recovery hit an unreadable thread…
    assert "sync" in events
    # …and recovery continued past it to notify the healthy thread
    assert ok.sent == [RECOVERY_NOTICE]
