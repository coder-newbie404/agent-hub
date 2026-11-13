"""Regression: /new must ack (defer) before its slow work.

followup.send on an interaction Discord never acked inside its 3s window
404s with 10015 (Unknown Webhook) — surfaced as "The application did not
respond". The ack must land before open_session/create_thread; early
rejections stay on interaction.response (pre-defer)."""

from types import SimpleNamespace

from discord_frontend.config import Settings
from discord_frontend.bot import SessionBot


class FakeResponse:
    def __init__(self, order):
        self.order = order

    async def defer(self, **kw):
        self.order.append("defer")

    async def send_message(self, text=None, **kw):
        self.order.append("send_message")


class FakeFollowup:
    def __init__(self, order):
        self.order = order

    async def send(self, *a, **kw):
        self.order.append("followup")


class FakeHub:
    def __init__(self, agents, fail_open_session=False):
        self._agents = agents
        self._fail = fail_open_session
        self.opened = False

    async def agents(self):
        return self._agents

    async def open_session(self, *a):
        self.opened = True
        if self._fail:
            raise RuntimeError("hub down")


def make_interaction(order):
    return SimpleNamespace(user=SimpleNamespace(id=42),
                           response=FakeResponse(order),
                           followup=FakeFollowup(order))


async def test_new_defers_before_open_session():
    order = []
    hub = FakeHub([SimpleNamespace(id="omp-local", online=True)],
                  fail_open_session=True)
    bot = SessionBot(Settings(discord_owner_id=42), hub)
    await bot._cmd_new(make_interaction(order), "t", None)
    assert hub.opened
    assert order.index("defer") < order.index("followup")
    # the token was consumed by defer, not by an early send_message
    assert "send_message" not in order


async def test_new_rejects_offline_agent_without_deferring():
    order = []
    hub = FakeHub([])  # no agent online
    bot = SessionBot(Settings(discord_owner_id=42), hub)
    await bot._cmd_new(make_interaction(order), "t", None)
    # _reject uses response.send_message; must run pre-defer or it would
    # raise InteractionResponded
    assert order == ["send_message"]
