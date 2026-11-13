"""Agent picking for /session new: explicit agent param wins, online[0]
fallback preserved; autocomplete lists online agents only."""
from types import SimpleNamespace

import pytest

from discord_frontend.config import Settings
from discord_frontend.bot import build_bot


class FakeResponse:
    def __init__(self):
        self.text = None
        self.embed = None
        self.deferred = False

    async def defer(self, **kwargs):
        self.deferred = True

    async def send_message(self, text=None, embed=None, view=None, **kwargs):
        self.text = text
        self.embed = embed


class FakeHub:
    def __init__(self, rows):
        self.rows = rows
        self.opened = []
        self.attached = None

    async def agents(self):
        return self.rows

    async def open_session(self, agent_id, user_id, workspace, title):
        self.opened.append(agent_id)
        return SimpleNamespace(id="sess_x", agent_id=agent_id, workspace=workspace,
                               title=title or "session", thread_id=None,
                               archived=False, status="created",
                               created_at=None, updated_at=None)

    async def attach_thread(self, session_id, thread_id):
        self.attached = (session_id, thread_id)


async def _thread_send(**kwargs):
    return None


async def _create_thread(**kwargs):
    return SimpleNamespace(id=999, send=_thread_send)


def make_bot(registry_rows):
    return build_bot(Settings(discord_owner_id=42),
                     FakeHub(registry_rows))


def make_interaction(uid=42):
    interaction = SimpleNamespace(user=SimpleNamespace(id=uid),
                                  response=FakeResponse(),
                                  followup=FakeResponse(),
                                  guild_id=111, channel=None)
    interaction.followup.send = \
        lambda **kw: FakeResponse.send_message(interaction.followup, **kw)
    interaction.channel = SimpleNamespace(create_thread=_create_thread)
    return interaction


def agent_row(agent_id, status="online", name=None):
    return SimpleNamespace(id=agent_id, name=name or agent_id,
                           status=status, online=status == "online",
                           capabilities="[]")


@pytest.fixture
def rows():
    return [agent_row("omp-local"),
            agent_row("google-agent", name="Google Agent")]


async def test_new_with_agent_targets_it(rows):
    bot = make_bot(rows)
    interaction = make_interaction()
    await bot._cmd_new(interaction, None, None, agent="google-agent")
    assert bot.hub.opened == ["google-agent"]


async def test_new_rejects_offline_or_unknown_agent(rows):
    bot = make_bot(rows)
    interaction = make_interaction()
    await bot._cmd_new(interaction, None, None, agent="ghost-agent")
    assert interaction.response.text is not None
    assert "ghost-agent" in interaction.response.text
    assert "offline" in interaction.response.text
    assert bot.hub.opened == []


async def test_new_without_agent_falls_back_to_first_online(rows):
    bot = make_bot(rows)
    interaction = make_interaction()
    await bot._cmd_new(interaction, None, None)
    assert bot.hub.opened == ["omp-local"]


async def test_new_no_agents_online_rejects():
    bot = make_bot([agent_row("omp-local", status="offline")])
    interaction = make_interaction()
    await bot._cmd_new(interaction, None, None)
    assert interaction.response.text == "no agent is online"
    assert bot.hub.opened == []


async def test_agent_autocomplete_lists_online_only(rows):
    bot = make_bot(rows)
    choices = await bot.agent_autocomplete(SimpleNamespace(), "google")
    assert [c.value for c in choices] == ["google-agent"]
    assert "Google Agent" in choices[0].name
