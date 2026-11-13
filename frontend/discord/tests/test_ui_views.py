"""Interaction-layer tests: dynamic buttons, modal, session picker — all
through fake interactions, no live Discord."""
import re
from types import SimpleNamespace

import discord

from discord_frontend.errors import NotFound, RunInProgress
from discord_frontend.ui import views

OWNER = 42


class FakeResponse:
    def __init__(self):
        self.text = None
        self.embed = None
        self.view = None
        self.ephemeral = None
        self.modal = None
        self.edits = []

    async def send_message(self, text=None, embed=None, view=None,
                           ephemeral=False):
        self.text, self.embed, self.view = text, embed, view
        self.ephemeral = ephemeral

    async def edit_message(self, **kw):
        self.edits.append(kw)

    async def send_modal(self, modal):
        self.modal = modal


def dc_error(exc_cls, status, code):
    """discord HTTPException with the minimal response attributes it reads."""
    resp = SimpleNamespace(status=status, reason=exc_cls.__name__)
    return exc_cls(resp, {"code": code, "message": "error"})


def not_found():
    return dc_error(discord.NotFound, 404, 10003)


def forbidden():
    return dc_error(discord.Forbidden, 403, 50001)



class FakeThread:
    def __init__(self):
        self.renamed = None
        self.id = 987

    async def edit(self, **kw):
        self.renamed = kw.get("name")

    async def send(self, **kw):
        self.sent = kw


class FakeChannel:
    def __init__(self):
        self.created = []

    async def create_thread(self, **kw):
        self.created.append(kw)
        return FakeThread()


class FakeSessions:
    def __init__(self, rows):
        self.rows = rows
        self.archived = []
        self.renamed = []
        self.cancel_calls = []
        self.deleted = []
        self.attached = []

    async def get_session(self, sid):
        if sid not in self.rows:
            raise NotFound(f"unknown session {sid}")
        return self.rows[sid]

    async def cancel(self, sid):
        self.cancel_calls.append(sid)
        return sid in self.rows and sid != "dead"

    async def delete_session(self, sid):
        if sid == "busy":
            raise RunInProgress("stop the run before deleting this session")
        if sid not in self.rows:
            raise NotFound(f"unknown session {sid}")
        self.deleted.append(sid)

    async def set_archived(self, sid, flag):
        self.archived.append((sid, flag))

    async def rename(self, sid, title):
        self.renamed.append((sid, title))

    async def message_count(self, sid):
        return 3

    async def attach_thread(self, sid, tid):
        self.attached.append((sid, tid))


class FakeBot:
    def __init__(self, rows=None, online=True, thread_error=None):
        self.settings = SimpleNamespace(discord_owner_id=OWNER)
        self.hub = FakeSessions(rows or {})
        self._online = online
        self.thread = FakeThread()
        self.thread_error = thread_error
        self.channel = FakeChannel()
        self.reprints = []

    async def _thread_row(self, row):
        if self.thread_error is not None:
            raise self.thread_error
        return self.thread

    async def agent_online(self, agent_id):
        return self._online

    async def reprint_transcript(self, row):
        self.reprints.append(row.id)
        return 0


def interaction(bot, uid=OWNER):
    return SimpleNamespace(client=bot, user=SimpleNamespace(id=uid),
                           response=FakeResponse(), guild_id=1,
                           channel=bot.channel)


def srow(sid="sess_1", **kw):
    base = dict(id=sid, agent_id="omp", status="running", archived=False,
                thread_id=555, title="OCR", updated_at=None, created_at=None,
                workspace=None)
    return SimpleNamespace(**{**base, **kw})


async def test_stop_button_owner_cancels_run():
    bot = FakeBot(rows={"sess_1": srow()})
    btn = await item_from(views.StopButton, "run:stop:sess_1")
    assert btn.sid == "sess_1"
    inter = interaction(bot)
    await btn.callback(inter)
    assert bot.hub.cancel_calls == ["sess_1"]
    assert inter.response.text == "🛑 stopping…"
    assert inter.response.ephemeral is True


async def test_stop_button_stranger_rejected():
    bot = FakeBot(rows={"sess_1": srow()})
    inter = interaction(bot, uid=99)
    await (await item_from(views.StopButton, "run:stop:sess_1")).callback(inter)
    assert inter.response.text == "not authorized"
    assert bot.hub.cancel_calls == []


async def test_rename_modal_renames_session_and_thread():
    bot = FakeBot(rows={"sess_1": srow()})
    modal = views.RenameModal("sess_1", "OCR")
    inter = interaction(bot)
    await modal.rename_to(inter, "  New name  ")
    assert bot.hub.renamed == [("sess_1", "New name")]
    assert bot.thread.renamed == "New name"
    assert "✏️" in inter.response.text


async def test_rename_modal_rejects_empty():
    bot = FakeBot(rows={"sess_1": srow()})
    modal = views.RenameModal("sess_1", "OCR")
    inter = interaction(bot)
    await modal.rename_to(inter, "   ")
    assert bot.hub.renamed == []
    assert "empty" in inter.response.text


async def test_archive_button_unknown_session():
    bot = FakeBot()
    inter = interaction(bot)
    await (await item_from(views.ArchiveButton,
                           "sess:archive:nope")).callback(inter)
    assert inter.response.text == "unknown session"
    assert bot.hub.archived == []


async def test_rename_button_opens_modal():
    bot = FakeBot(rows={"sess_1": srow()})
    inter = interaction(bot)
    await (await item_from(views.RenameButton,
                           "sess:rename:sess_1")).callback(inter)
    modal = inter.response.modal
    assert isinstance(modal, views.RenameModal)
    assert modal.sid == "sess_1"


def test_run_controls_view_capability_gating():
    assert views.run_controls_view("s1", can_stop=False) is None
    ids = _component_ids(views.run_controls_view("s1", can_stop=True))
    assert ids == ["run:stop:s1"]


async def test_session_card_view_buttons():
    ids = _component_ids(views.session_card_view("s1", thread_url=None))
    assert "sess:rename:s1" in ids
    assert "sess:archive:s1" in ids
    assert "sess:delete:s1" in ids
    linked = _flat(views.session_card_view("s1", thread_url="https://x/1"))
    assert any(c.get("url") == "https://x/1" for c in linked)


def test_picker_pagination():
    rows = [srow(f"s{i}") for i in range(views.PICK_PAGE_SIZE + 5)]
    picker = views.SessionPickerView(FakeBot(), rows, guild_id=1)
    assert len(picker.select.options) == views.PICK_PAGE_SIZE
    picker.flip(1)
    assert len(picker.select.options) == 5
    assert picker.embed().description.count("s5") >= 0
    picker.flip(-1)
    assert len(picker.select.options) == views.PICK_PAGE_SIZE
    picker.flip(-1)  # already on first page — stays clamped
    assert picker.page == 0


async def test_picker_selection_edits_private_card():
    rows = [srow(f"s{i}") for i in range(3)]
    bot = FakeBot(rows={r.id: r for r in rows})
    picker = views.SessionPickerView(bot, rows, guild_id=1)
    inter = interaction(bot)
    await picker.pick("s1", inter)
    assert inter.response.edits, "selection must edit the private card"
    kw = inter.response.edits[0]
    assert "OCR" in kw["embed"].title
    assert "sess:opennew:s1" in _component_ids(kw["view"])


def _component_ids(view):
    return [c["custom_id"] for comp in view.to_components()
            for c in comp["components"] if "custom_id" in c]



async def test_picker_dead_thread_offers_relink():
    rows = [srow(f"s{i}") for i in range(3)]
    bot = FakeBot(rows={r.id: r for r in rows},
                  thread_error=not_found())
    picker = views.SessionPickerView(bot, rows, guild_id=1)
    inter = interaction(bot)
    await picker.pick("s1", inter)
    kw = inter.response.edits[0]
    assert "sess:relink:s1" in _component_ids(kw["view"])


async def test_picker_deleted_thread_hides_dead_link():
    rows = [srow()]
    bot = FakeBot(rows={"sess_1": rows[0]},
                  thread_error=not_found())
    picker = views.SessionPickerView(bot, rows, guild_id=1)
    inter = interaction(bot)
    await picker.pick("sess_1", inter)
    kw = inter.response.edits[0]
    for c in _flat(kw["view"]):
        assert "url" not in c, "dead jump link must not be served"


async def test_picker_threadless_session_offers_relink():
    rows = [srow("bare", thread_id=None)]
    bot = FakeBot(rows={"bare": rows[0]})
    picker = views.SessionPickerView(bot, rows, guild_id=1)
    inter = interaction(bot)
    await picker.pick("bare", inter)
    assert "sess:relink:bare" in _component_ids(inter.response.edits[0]["view"])


async def test_picker_unreadable_thread_keeps_link():
    """Forbidden (bot-unreadable) threads may still exist for the user —
    serve the jump link; never relink over a possibly-live thread."""
    rows = [srow()]
    bot = FakeBot(rows={"sess_1": rows[0]},
                  thread_error=forbidden())
    picker = views.SessionPickerView(bot, rows, guild_id=1)
    inter = interaction(bot)
    await picker.pick("sess_1", inter)
    kw = inter.response.edits[0]
    assert any("url" in c for c in _flat(kw["view"]))
    assert "sess:relink:sess_1" not in _component_ids(kw["view"])

async def test_relink_creates_thread_and_moves_session():
    row = srow()
    bot = FakeBot(rows={"sess_1": row})
    inter = interaction(bot)
    await (await item_from(views.RelinkButton,
                           "sess:relink:sess_1")).callback(inter)
    assert bot.channel.created, "fresh thread must be created"
    assert bot.hub.attached == [("sess_1", 987)]
    assert row.thread_id == 987
    kw = inter.response.edits[0]
    assert any("url" in c for c in _flat(kw["view"])), "fresh jump link served"
    assert bot.reprints == ["sess_1"]
async def test_relink_acks_before_reprint():
    """Interaction tokens die after 3 s: the card swap is the ack, so a
    full transcript reprint must run only after it (shipped-as-bug guard:
    reprint-then-ack made Discord show 'did not respond' on big sessions)."""
    row = srow()
    bot = FakeBot(rows={"sess_1": row})
    inter = interaction(bot)
    order: list[str] = []
    real_edit = inter.response.edit_message

    async def edit(**kw):
        order.append("ack")
        await real_edit(**kw)

    inter.response.edit_message = edit

    async def reprint(r):
        order.append("reprint")
        return 0

    bot.reprint_transcript = reprint
    await (await item_from(views.RelinkButton,
                           "sess:relink:sess_1")).callback(inter)
    assert order == ["ack", "reprint"]



async def test_thread_deleted_probe():
    dead = FakeBot(thread_error=not_found())
    assert await views.thread_deleted(dead, srow(thread_id=555)) is True
    assert await views.thread_deleted(dead, srow(thread_id=None)) is True
    assert await views.thread_deleted(FakeBot(), srow(thread_id=555)) is False
    assert await views.thread_deleted(FakeBot(thread_error=forbidden()),
                                      srow(thread_id=555)) is False

def _flat(view):
    return [c for comp in view.to_components() for c in comp["components"]]


def item_from(item_cls, custom_id):
    match = item_cls.__discord_ui_compiled_template__.match(custom_id)
    assert match, f"{item_cls.__name__} template rejects {custom_id}"
    return item_cls.from_custom_id(None, None, match)


async def test_delete_button_only_arms_confirm():
    bot = FakeBot(rows={"sess_1": srow()})
    inter = interaction(bot)
    await (await item_from(views.DeleteButton,
                           "sess:delete:sess_1")).callback(inter)
    assert bot.hub.deleted == []  # nothing deleted yet — armed only
    view = inter.response.edits[-1]["view"]
    assert {"sess:delconfirm:sess_1", "sess:delcancel:sess_1"} <= {
        c["custom_id"] for c in _flat(view) if "custom_id" in c}


async def test_confirm_deletes_and_strips_controls():
    bot = FakeBot(rows={"sess_1": srow()})
    inter = interaction(bot)
    await (await item_from(views.DeleteConfirmButton,
                           "sess:delconfirm:sess_1")).callback(inter)
    assert bot.hub.deleted == ["sess_1"]
    kw = inter.response.edits[-1]
    assert kw["view"] is None
    assert "deleted" in kw["embed"].description


async def test_confirm_refuses_inflight_session():
    bot = FakeBot(rows={"busy": srow("busy")})
    inter = interaction(bot)
    await (await item_from(views.DeleteConfirmButton,
                           "sess:delconfirm:busy")).callback(inter)
    assert bot.hub.deleted == []
    assert "stop the run" in inter.response.text
    assert inter.response.ephemeral is True


async def test_confirm_unknown_session():
    bot = FakeBot()
    inter = interaction(bot)
    await (await item_from(views.DeleteConfirmButton,
                           "sess:delconfirm:nope")).callback(inter)
    assert bot.hub.deleted == []
    assert inter.response.text == "unknown session"


async def test_cancel_reverts_to_session_card():
    bot = FakeBot(rows={"sess_1": srow()})
    inter = interaction(bot)
    await (await item_from(views.DeleteCancelButton,
                           "sess:delcancel:sess_1")).callback(inter)
    kw = inter.response.edits[-1]
    assert "OCR" in kw["embed"].title  # card re-rendered
    assert "sess:delete:sess_1" in _component_ids(kw["view"])


async def test_open_thread_button_creates_and_reprints():
    bot = FakeBot(rows={"s1": srow("s1", thread_id=None)})
    inter = interaction(bot)
    await (await item_from(views.OpenThreadButton,
                           "sess:opennew:s1")).callback(inter)
    assert bot.channel.created, "fresh thread must be created"
    assert bot.hub.attached == [("s1", 987)]
    assert bot.hub.rows["s1"].thread_id == 987
    assert bot.reprints == ["s1"]
