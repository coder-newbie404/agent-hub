"""Discord components: persistent action buttons (DynamicItem), the rename
modal, and the ephemeral session picker.

Interaction rules:
- Every handler re-resolves session state from the DB by id, so buttons keep
  working after a manager restart (no captured Python state).
- Every mutating action is owner-gated, matching the slash commands.
- Controls the agent does not support are never rendered (roadmap §11).

The custom_id namespaces (`run:stop:*`, `sess:*:*`, and a future `ask:*`)
are the seam where a later ui.request/ui.response layer plugs in: its
selection/input cards render as private (ephemeral) cards built from the
same vocabulary used here."""

import logging
import math

import discord

from discord_frontend.errors import NotFound, RunInProgress
from discord_frontend.ui import embeds, icons

logger = logging.getLogger(__name__)

PICK_PAGE_SIZE = 25  # Discord hard limit on select-menu options
PICKER_TIMEOUT = 180.0
_STREAM = "https://discord.com/channels"


async def _gate(interaction) -> bool:
    bot = interaction.client
    if interaction.user.id != bot.settings.discord_owner_id:
        await interaction.response.send_message("not authorized",
                                                ephemeral=True)
        return False
    return True


class StopButton(discord.ui.DynamicItem[discord.ui.Button],
                 template=r"run:stop:(?P<sid>[A-Za-z0-9_-]+)"):
    def __init__(self, sid: str):
        super().__init__(discord.ui.Button(label="Stop", emoji=icons.STOP,
                                           style=discord.ButtonStyle.danger,
                                           custom_id=f"run:stop:{sid}"))
        self.sid = sid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["sid"])

    async def callback(self, interaction):
        if not await _gate(interaction):
            return
        bot = interaction.client
        if not await bot.hub.cancel(self.sid):
            await interaction.response.send_message("nothing to stop",
                                                    ephemeral=True)
            return
        await interaction.response.send_message(f"{icons.STOP} stopping…",
                                                ephemeral=True)


class ArchiveButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"sess:archive:(?P<sid>[A-Za-z0-9_-]+)"):
    def __init__(self, sid: str):
        super().__init__(discord.ui.Button(label="Archive",
                                           emoji=icons.ARCHIVE,
                                           style=discord.ButtonStyle.secondary,
                                           custom_id=f"sess:archive:{sid}"))
        self.sid = sid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["sid"])

    async def callback(self, interaction):
        if not await _gate(interaction):
            return
        bot = interaction.client
        try:
            row = await bot.hub.get_session(self.sid)  # existence check first
        except NotFound:
            await interaction.response.send_message("unknown session",
                                                    ephemeral=True)
            return
        await bot.hub.set_archived(self.sid, True)
        card = embeds.session_card(
            await bot.hub.get_session(self.sid),
            online=await bot.agent_online(row.agent_id),
            messages=await bot.hub.message_count(self.sid))
        await interaction.response.edit_message(embed=card)


class RenameButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"sess:rename:(?P<sid>[A-Za-z0-9_-]+)"):
    def __init__(self, sid: str):
        super().__init__(discord.ui.Button(label="Rename", emoji=icons.RENAME,
                                           style=discord.ButtonStyle.secondary,
                                           custom_id=f"sess:rename:{sid}"))
        self.sid = sid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["sid"])

    async def callback(self, interaction):
        if not await _gate(interaction):
            return
        bot = interaction.client
        try:
            row = await bot.hub.get_session(self.sid)
        except NotFound:
            await interaction.response.send_message("unknown session",
                                                    ephemeral=True)
            return
        await interaction.response.send_modal(
            RenameModal(self.sid, embeds.display_title(row)))


class DeleteButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"sess:delete:(?P<sid>[A-Za-z0-9_-]+)"):
    """First press only arms the destructive action: the card swaps to a
    Confirm/Cancel pair. Nothing is deleted until Confirm."""

    def __init__(self, sid: str):
        super().__init__(discord.ui.Button(label="Delete", emoji=icons.DELETE,
                                           style=discord.ButtonStyle.danger,
                                           custom_id=f"sess:delete:{sid}"))
        self.sid = sid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["sid"])

    async def callback(self, interaction):
        if not await _gate(interaction):
            return
        bot = interaction.client
        try:
            await bot.hub.get_session(self.sid)
        except NotFound:
            await interaction.response.send_message("unknown session",
                                                    ephemeral=True)
            return
        confirm = discord.ui.View(timeout=None)
        confirm.add_item(DeleteConfirmButton(self.sid))
        confirm.add_item(DeleteCancelButton(self.sid))
        await interaction.response.edit_message(view=confirm)


class DeleteConfirmButton(discord.ui.DynamicItem[discord.ui.Button],
                          template=r"sess:delconfirm:(?P<sid>[A-Za-z0-9_-]+)"):
    def __init__(self, sid: str):
        super().__init__(discord.ui.Button(label="Confirm delete",
                                           emoji=icons.CONFIRM,
                                           style=discord.ButtonStyle.danger,
                                           custom_id=f"sess:delconfirm:{sid}"))
        self.sid = sid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["sid"])

    async def callback(self, interaction):
        if not await _gate(interaction):
            return
        bot = interaction.client
        try:
            await bot.hub.delete_session(self.sid)
        except RunInProgress:
            await interaction.response.send_message(
                "stop the run before deleting this session", ephemeral=True)
            return
        except NotFound:
            await interaction.response.send_message("unknown session",
                                                    ephemeral=True)
            return
        notice = discord.Embed(
            description=f"{icons.DELETE} deleted session `{self.sid}`"
                        " (thread kept)",
            color=embeds.STATUS_COLOR["failed"])
        await interaction.response.edit_message(embed=notice, view=None)


class DeleteCancelButton(discord.ui.DynamicItem[discord.ui.Button],
                         template=r"sess:delcancel:(?P<sid>[A-Za-z0-9_-]+)"):
    def __init__(self, sid: str):
        super().__init__(discord.ui.Button(label="Cancel", emoji=icons.CANCEL,
                                           style=discord.ButtonStyle.secondary,
                                           custom_id=f"sess:delcancel:{sid}"))
        self.sid = sid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["sid"])

    async def callback(self, interaction):
        if not await _gate(interaction):
            return
        bot = interaction.client
        try:
            row = await bot.hub.get_session(self.sid)
        except NotFound:
            await interaction.response.send_message("unknown session",
                                                    ephemeral=True)
            return
        url = None
        if row.thread_id is not None and interaction.guild_id is not None:
            url = (f"https://discord.com/channels/"
                   f"{interaction.guild_id}/{row.thread_id}")
        card = embeds.session_card(
            row, online=await bot.agent_online(row.agent_id),
            messages=await bot.hub.message_count(self.sid))
        await interaction.response.edit_message(
            embed=card, view=session_card_view(self.sid, url))


class RelinkButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"sess:relink:(?P<sid>[A-Za-z0-9_-]+)"):
    """A session whose thread was deleted: create a fresh thread and move
    this session's link onto it. Transcript and resume state survive —
    they are keyed by session id, not thread."""

    def __init__(self, sid: str):
        super().__init__(discord.ui.Button(label="New thread",
                                           emoji=icons.NEW_THREAD,
                                           custom_id=f"sess:relink:{sid}"))
        self.sid = sid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["sid"])

    async def callback(self, interaction):
        if not await _gate(interaction):
            return
        bot = interaction.client
        try:
            row = await bot.hub.get_session(self.sid)
        except NotFound:
            await interaction.response.send_message("unknown session",
                                                    ephemeral=True)
            return
        try:
            thread = await interaction.channel.create_thread(
                name=embeds.display_title(row)[:100],
                type=discord.ChannelType.public_thread)
        except Exception as exc:
            logger.warning("thread relink failed for session %s", self.sid,
                           exc_info=True)
            await interaction.response.send_message(
                f"thread create failed: {exc}", ephemeral=True)
            return
        await bot.hub.attach_thread(row.id, thread.id)
        row.thread_id = thread.id
        # ack first: interaction tokens die after 3 s, and a full transcript
        # reprint runs minutes — the button's card swap must not wait on it
        await interaction.response.edit_message(
            embed=embeds.jump_card(row),
            view=thread_link_view(thread.id, interaction.guild_id))
        try:
            await bot.reprint_transcript(row)
        except Exception:
            logger.warning("transcript reprint failed for session %s",
                           self.sid, exc_info=True)


class OpenThreadButton(discord.ui.DynamicItem[discord.ui.Button],
                       template=r"sess:opennew:(?P<sid>[A-Za-z0-9_-]+)"):
    """Bind an old session to a brand-new thread and reprint its full
    transcript there (user + agent messages)."""

    def __init__(self, sid: str):
        super().__init__(discord.ui.Button(label="Open in new thread",
                                           emoji=icons.NEW_THREAD,
                                           custom_id=f"sess:opennew:{sid}"))
        self.sid = sid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["sid"])

    async def callback(self, interaction):
        if not await _gate(interaction):
            return
        bot = interaction.client
        try:
            row = await bot.hub.get_session(self.sid)
        except NotFound:
            await interaction.response.send_message("unknown session",
                                                    ephemeral=True)
            return
        try:
            thread = await interaction.channel.create_thread(
                name=embeds.display_title(row)[:100],
                type=discord.ChannelType.public_thread)
        except Exception as exc:
            logger.warning("thread open failed for session %s", self.sid,
                           exc_info=True)
            await interaction.response.send_message(
                f"thread create failed: {exc}", ephemeral=True)
            return
        await bot.hub.attach_thread(row.id, thread.id)
        row.thread_id = thread.id
        await interaction.response.edit_message(
            embed=embeds.jump_card(row),
            view=thread_link_view(thread.id, interaction.guild_id))
        await bot.reprint_transcript(row)


class RenameModal(discord.ui.Modal):
    def __init__(self, sid: str, current_title: str):
        super().__init__(title="Rename session")
        self.sid = sid
        self.title_input = discord.ui.TextInput(label="Title",
                                                default=current_title or "",
                                                max_length=100)
        self.add_item(self.title_input)

    async def on_submit(self, interaction):
        await self.rename_to(interaction, self.title_input.value)

    async def rename_to(self, interaction, raw: str):
        """Submission logic; split from on_submit so it is testable without
        Discord's TextInput plumbing."""
        if not await _gate(interaction):
            return
        bot = interaction.client
        title = raw.strip()[:100]
        if not title:
            await interaction.response.send_message(
                "title must not be empty", ephemeral=True)
            return
        await bot.hub.rename(self.sid, title)
        try:
            row = await bot.hub.get_session(self.sid)
        except NotFound:
            row = None
        thread = await bot._thread_row(row) if row is not None else None
        if thread is not None:
            try:
                await thread.edit(name=title)
            except Exception:
                logger.warning("thread rename failed for session %s",
                               self.sid, exc_info=True)
        await interaction.response.send_message(f"{icons.RENAME} renamed to"
                                                f" {title}", ephemeral=True)


def run_controls_view(sid: str, can_stop: bool) -> discord.ui.View | None:
    if not can_stop:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(StopButton(sid))
    return view


def session_card_view(sid: str, thread_url: str | None) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if thread_url:
        view.add_item(discord.ui.Button(label="Open thread",
                                        emoji=icons.OPEN_THREAD,
                                        url=thread_url))
    view.add_item(RenameButton(sid))
    view.add_item(ArchiveButton(sid))
    view.add_item(DeleteButton(sid))
    return view


def thread_link_view(thread_id: int | None,
                     guild_id: int | None) -> discord.ui.View | None:
    if thread_id is None or guild_id is None:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Open thread",
                                    emoji=icons.OPEN_THREAD,
                                    url=f"{_STREAM}/{guild_id}/{thread_id}"))
    return view


async def thread_deleted(bot, row) -> bool:
    """True when the session's linked thread is absent or confirmed deleted:
    a dead link must never be served, and a fresh thread may take over.
    Forbidden (bot-unreadable) threads may still exist for the user — they
    are treated as alive here."""
    if row.thread_id is None:
        return True
    try:
        await bot._thread_row(row)
    except discord.NotFound:
        return True
    except discord.Forbidden:
        return False  # unreadable, but may still exist for the user
    return False


def relink_view(sid: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(RelinkButton(sid))
    return view




class SessionPickerView(discord.ui.View):
    """Private (ephemeral) session selection card: embed + select menu with
    pagination; picking edits the card to a jump card."""

    def __init__(self, bot, rows, guild_id: int | None):
        super().__init__(timeout=PICKER_TIMEOUT)
        self.bot = bot
        self.rows = list(rows)  # recent-first, non-archived
        self.guild_id = guild_id
        self.page = 0
        self.pages = max(1, math.ceil(len(self.rows) / PICK_PAGE_SIZE))
        self.select = discord.ui.Select(placeholder="Pick a session…")
        self.select.callback = self.on_pick
        self.add_item(self.select)
        self.prev = discord.ui.Button(emoji="◀️",
                                      style=discord.ButtonStyle.secondary,
                                      custom_id="pick:prev")
        self.next = discord.ui.Button(emoji="▶️",
                                      style=discord.ButtonStyle.secondary,
                                      custom_id="pick:next")
        self.prev.callback = self.on_prev
        self.next.callback = self.on_next
        self.add_item(self.prev)
        self.add_item(self.next)
        self._refresh()

    def _slice(self):
        start = self.page * PICK_PAGE_SIZE
        return self.rows[start:start + PICK_PAGE_SIZE]

    def _refresh(self):
        self.select.options.clear()
        for r in self._slice():
            mark = f" {icons.ARCHIVED_ICON}" if r.archived else ""
            desc = (f"{icons.status_icon(r.status)}{mark} {r.status}"
                    f" · {embeds.ago(r.updated_at)}")[:100]
            self.select.add_option(label=embeds.display_title(r)[:100],
                                   value=r.id, description=desc)
        self.prev.disabled = self.page == 0
        self.next.disabled = self.page >= self.pages - 1

    def flip(self, delta: int) -> None:
        self.page = min(max(self.page + delta, 0), self.pages - 1)
        self._refresh()

    def embed(self) -> discord.Embed:
        return embeds.session_list_embed(self._slice(), page=self.page,
                                         pages=self.pages)

    async def on_prev(self, interaction):
        self.flip(-1)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def on_next(self, interaction):
        self.flip(1)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def on_pick(self, interaction):
        values = getattr(self.select, "values", None) or []
        if values:
            await self.pick(values[0], interaction)

    async def pick(self, sid: str, interaction):
        try:
            row = await self.bot.hub.get_session(sid)
        except NotFound:
            await interaction.response.edit_message(
                embed=discord.Embed(description="unknown session"), view=None)
            return
        if await thread_deleted(self.bot, row):
            await interaction.response.edit_message(
                embed=embeds.missing_thread_card(row),
                view=relink_view(row.id))
            return
        view = thread_link_view(row.thread_id, self.guild_id) or \
            discord.ui.View(timeout=None)
        view.add_item(OpenThreadButton(row.id))
        await interaction.response.edit_message(embed=embeds.jump_card(row),
                                                view=view)


def setup(bot) -> None:
    """Register persistent dynamic items so buttons survive restarts."""
    bot.add_dynamic_items(StopButton, ArchiveButton, RenameButton,
                          DeleteButton, DeleteConfirmButton,
                          DeleteCancelButton, RelinkButton, OpenThreadButton)
