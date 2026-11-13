from types import SimpleNamespace

from discord import app_commands

from discord_frontend.config import Settings
from discord_frontend.bot import SessionBot, ensure_owner


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeInteraction:
    def __init__(self, uid):
        self.user = FakeUser(uid)


def test_owner_passes():
    assert ensure_owner(FakeInteraction(42), 42) is True


def test_stranger_rejected():
    assert ensure_owner(FakeInteraction(43), 42) is False


class FakeResponse:
    def __init__(self):
        self.text = None
        self.embed = None

    async def send_message(self, text=None, embed=None, view=None,
                           ephemeral=False):
        self.text = text
        self.embed = embed

from discord_frontend.bot import build_bot


def make_bot() -> SessionBot:
    # commands are attached by build_bot, not SessionBot itself
    return build_bot(Settings(discord_owner_id=42),
                     SimpleNamespace())


def make_interaction(uid: int):
    return SimpleNamespace(user=SimpleNamespace(id=uid), response=FakeResponse())


async def test_help_lists_every_registered_command():
    bot = make_bot()
    interaction = make_interaction(42)
    await bot._cmd_help(interaction)
    # /help is an embed now; the command list lives in the description
    embed = interaction.response.embed
    assert embed is not None, "/help must send an embed"
    text = embed.description

    # sync invariant: help must mention every invocable command
    names = set()
    for cmd in bot.tree.get_commands():
        if isinstance(cmd, app_commands.Group):
            names.update(f"/{c.qualified_name}" for c in cmd.commands)
        else:
            names.add(f"/{cmd.name}")
    assert names, "bot registers no commands?"
    for name in names:
        assert name in text, f"{name} missing from /help"


async def test_help_rejects_stranger():
    bot = make_bot()
    interaction = make_interaction(43)
    await bot._cmd_help(interaction)
    assert interaction.response.text == "not authorized"
