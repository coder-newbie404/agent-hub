"""The Discord frontend: threads ↔ hub sessions.

Formerly manager/app/discord_bot.py. Every hub touch goes through the
HubClient (REST); session/sync events arrive over the /ws/ui stream —
the same events the in-process bot used to receive through the notifier
callback slot."""
import asyncio
import logging
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from discord_frontend.chunking import chunk_message
from discord_frontend.errors import AgentOffline, NotFound, RunInProgress
from discord_frontend.ui import embeds, icons, views
from discord_frontend.ui.embeds import display_title

logger = logging.getLogger(__name__)

QUOTE_CHARS = 400
SYNC_PACING = 0.4
# hub session vocabulary (stable wire values; sessions.py owns the source)
RUNNING, WAITING = "running", "waiting"


def ensure_owner(interaction, owner_id: int) -> bool:
    return interaction.user.id == owner_id


def compose_prompt(text: str, quoted_author: str | None,
                   quoted_content: str | None) -> str:
    """A Discord reply quotes the earlier message as explicit context."""
    if not quoted_content:
        return text
    quote = quoted_content.strip()
    if len(quote) > QUOTE_CHARS:
        quote = quote[:QUOTE_CHARS].rstrip() + "…"
    author = quoted_author or "user"
    return f"> {author}: {quote}\n\n{text}"


class _Live:
    """One run card per in-flight run; edited in place while streaming."""

    def __init__(self):
        self.message = None
        self.draft = ""
        self.activity: str | None = None
        self.dirty = False
        self.started = time.monotonic()
        self.started_at = datetime.now(timezone.utc)
        self.title = "session"
        self.agent_id = "?"


class SessionBot(commands.Bot):
    def __init__(self, settings, hub):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.hub = hub
        self._events_task: asyncio.Task | None = None
        self._typing: dict[str, asyncio.Task] = {}
        self._live: dict[str, _Live] = {}
        self._flushers: dict[str, asyncio.Task] = {}
        self._sync_tasks: set[asyncio.Task] = set()
        # serializes print_missed per session: overlapping runs would
        # double-print the transcript and auto-create duplicate threads
        self._print_locks: dict[str, asyncio.Lock] = {}

    async def setup_hook(self) -> None:
        # sync first: recovery trouble below must not skip command registration
        if self.settings.discord_guild_id:
            guild = discord.Object(self.settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        # persistent component actions (run stop, session rename/archive)
        views.setup(self)
        # restart recovery: mark in-flight runs failed and note their threads;
        # one unreadable (e.g. deleted) thread must not abort the rest
        for sid in await self.hub.recover_interrupted():
            try:
                row = await self.hub.get_session(sid)
                thread = await self._thread_row(row)
                if thread is not None:
                    await thread.send("manager restarted; the in-flight run was lost")
            except Exception:
                logger.warning("recovery notice failed for session %s", sid,
                               exc_info=True)
        # hub → frontend event stream (replaces the in-process notifier)
        self._events_task = asyncio.create_task(self._run_events())

    async def close(self) -> None:
        if self._events_task is not None:
            self._events_task.cancel()
        await super().close()

    # --- hub event stream ---------------------------------------------------

    async def _run_events(self) -> None:
        async for frame in self.hub.events():
            try:
                await self._dispatch_event(frame)
            except Exception:
                logger.warning("event dispatch failed for %s",
                               frame.get("type"), exc_info=True)

    async def _dispatch_event(self, frame: dict) -> None:
        kind = frame.get("type")
        sid = frame.get("session_id")
        if kind == "session.message":
            await self._deliver_session_message(sid, frame["text"],
                                                frame["message_id"])
        elif kind == "session.delta":
            self._on_delta(sid, frame["draft"])
        elif kind == "session.activity":
            self._on_activity(sid, frame["label"])
        elif kind == "session.completed":
            await self._finish_request(sid)
            await self._finalize_run(sid, "completed")
        elif kind == "session.failed":
            reason = frame["reason"]
            await self._finish_request(sid)
            status = "cancelled" if reason == "cancelled" else "failed"
            await self._finalize_run(sid, status, reason)
            thread = await self._thread_for(sid)
            if thread is not None:
                try:
                    await thread.send(f"run failed: {reason}")
                except Exception:
                    logger.warning("failed to post failure notice for session %s",
                                   sid, exc_info=True)
        elif kind == "agent.disconnected":
            await self._agent_disconnected(frame["agent_id"])
        elif kind == "sync.applied":
            await self.on_sync_applied(frame["agent_id"],
                                       frame["session_ids"])

    async def _deliver_session_message(self, session_id: str, text: str,
                                       message_id: int) -> None:
        """Final agent reply → session thread; stays unposted on failure
        (the missed-message sync retries it)."""
        thread = await self._thread_for(session_id)
        if thread is None:
            return
        try:
            for piece in chunk_message(text):
                await thread.send(piece)
        except Exception:
            logger.warning("failed to post message chunk for session %s",
                           session_id, exc_info=True)
            return  # stays unposted; sync retries it
        try:
            await self.hub.mark_posted(session_id, [message_id])
        except Exception:
            logger.warning("posted marking failed for %s", message_id,
                           exc_info=True)

    async def _agent_disconnected(self, agent_id: str) -> None:
        """Projection-mode agents: report unknown state, never a verdict."""
        for row in await self.hub.list_sessions():
            if row.agent_id != agent_id or row.status not in (RUNNING, WAITING):
                continue
            thread = await self._thread_for(row.id)
            if thread is not None:
                try:
                    await thread.send("agent disconnected; run state unknown"
                                      " until it reconnects")
                except Exception:
                    logger.warning("disconnect notice failed for session %s",
                                   row.id, exc_info=True)

    # --- hub lookups ----------------------------------------------------------

    async def agent_online(self, agent_id: str) -> bool:
        try:
            rows = await self.hub.agents()
        except Exception:
            return False
        return any(r.id == agent_id and r.online for r in rows)

    async def agent_capabilities(self, agent_id: str) -> set[str]:
        for r in await self.hub.agents():
            if r.id == agent_id:
                return set(embeds.parse_caps(r.capabilities))
        return set()

    async def _session_for_thread(self, thread_id: int):
        try:
            return await self.hub.session_by_thread(thread_id)
        except NotFound:
            return None

    async def _thread_row(self, row):
        if row.thread_id is None:
            return None
        return self.get_channel(row.thread_id) or await self.fetch_channel(row.thread_id)

    async def _thread_for(self, session_id: str):
        try:
            row = await self.hub.get_session(session_id)
        except NotFound:
            return None
        return await self._thread_row(row)

    async def _start_typing(self, session_id: str) -> None:
        # cancel any hold this session already has: a second _start_typing
        # must not overwrite the map entry and leak the displaced task
        await self._finish_request(session_id)
        thread = await self._thread_for(session_id)
        if thread is None:
            return

        async def hold():
            async with thread.typing():
                while session_id in self._typing:
                    await asyncio.sleep(1)

        self._typing[session_id] = asyncio.create_task(hold())

    async def _finish_request(self, session_id: str) -> None:
        task = self._typing.pop(session_id, None)
        if task:
            task.cancel()

    # --- run card (live streaming) ----------------------------------------

    def _on_delta(self, session_id: str, draft: str) -> None:
        state = self._live.setdefault(session_id, _Live())
        state.draft = draft
        state.dirty = True
        self._ensure_flusher(session_id)

    def _on_activity(self, session_id: str, label: str | None) -> None:
        state = self._live.setdefault(session_id, _Live())
        state.activity = label
        if state.message is not None or state.draft:
            state.dirty = True
            self._ensure_flusher(session_id)

    def _ensure_flusher(self, session_id: str) -> None:
        if session_id in self._flushers:
            return
        self._flushers[session_id] = asyncio.create_task(
            self._flush_loop(session_id))

    async def _flush_loop(self, session_id: str) -> None:
        """Edit the run card at most once per stream_interval: Discord
        rate-limits message edits hard, so individual deltas must not hit
        the API."""
        try:
            while True:
                await asyncio.sleep(self.settings.stream_interval)
                state = self._live.get(session_id)
                if state is None or not state.dirty:
                    continue
                state.dirty = False
                thread = await self._thread_for(session_id)
                if thread is None:
                    return
                try:
                    if state.message is None:
                        row = await self.hub.get_session(session_id)
                        state.title = display_title(row)
                        state.agent_id = row.agent_id
                        # capability-gated controls (roadmap §11)
                        can_stop = "cancel" in await self.agent_capabilities(
                            row.agent_id)
                        state.message = await thread.send(
                            embed=embeds.run_card(title=state.title,
                                                  agent_id=state.agent_id,
                                                  status="running",
                                                  draft=state.draft,
                                                  activity=state.activity,
                                                  started_at=state.started_at),
                            view=views.run_controls_view(session_id, can_stop))
                    else:
                        await state.message.edit(
                            embed=embeds.run_card(title=state.title,
                                                  agent_id=state.agent_id,
                                                  status="running",
                                                  draft=state.draft,
                                                  activity=state.activity,
                                                  started_at=state.started_at))
                except NotFound:
                    self._live.pop(session_id, None)
                    return
                except Exception:
                    logger.warning("run card update failed for session %s",
                                   session_id, exc_info=True)
                    self._live.pop(session_id, None)
                    return
        except asyncio.CancelledError:
            pass
        finally:
            self._flushers.pop(session_id, None)

    async def _finalize_run(self, session_id: str, status: str,
                            reason: str | None = None) -> None:
        """Flip the run card to its terminal state and strip the controls;
        the final (chunked) reply still follows separately."""
        state = self._live.pop(session_id, None)
        task = self._flushers.pop(session_id, None)
        if task:
            task.cancel()
        if state is None or state.message is None:
            return
        try:
            await state.message.edit(
                embed=embeds.run_card(title=state.title,
                                      agent_id=state.agent_id,
                                      status=status,
                                      draft=state.draft,
                                      activity=state.activity,
                                      elapsed=time.monotonic() - state.started,
                                      reason=reason),
                view=None)
        except Exception:
            logger.warning("run card finalize failed for session %s",
                           session_id, exc_info=True)

    # --- sync rendering ---------------------------------------------------

    @staticmethod
    def _prefix(role: str, content: str) -> str:
        who = "**You:**" if role == "user" else "**Agent:**"
        return f"{who}\n{content}"

    async def _ensure_thread(self, row):
        """Existing linked thread, or a fresh public thread in the sync
        channel for threadless (local) sessions; None when neither works."""
        thread = await self._thread_for(row.id)
        if thread is not None:
            return thread
        if not self.settings.discord_sync_channel_id:
            return None
        channel = self.get_channel(self.settings.discord_sync_channel_id) \
            or await self.fetch_channel(self.settings.discord_sync_channel_id)
        try:
            thread = await channel.create_thread(
                name=display_title(row)[:100],
                type=discord.ChannelType.public_thread)
        except Exception:
            logger.warning("auto-thread create failed for session %s",
                           row.id, exc_info=True)
            return None
        await self.hub.attach_thread(row.id, thread.id)
        row.thread_id = thread.id
        await thread.send(
            embed=embeds.session_card(
                row, online=await self.agent_online(row.agent_id),
                messages=await self.hub.message_count(row.id)),
            view=views.session_card_view(row.id, None))
        return thread

    async def _send_lines(self, thread, lines,
                          label: str = "message") -> bool:
        """Post (prefix, content) pairs chunked, with rate-limit pacing.
        Returns False on a non-retryable client error (4xx ≠ 429): the
        rejection is logged and the caller gives up on this message
        instead of retrying forever."""
        for who, text in lines:
            try:
                for piece in chunk_message(self._prefix(who, text)):
                    await thread.send(piece)
                    await asyncio.sleep(SYNC_PACING)
            except discord.HTTPException as exc:
                status = getattr(exc, "status", None) or (
                    exc.response.status if exc.response else None)
                if status and 400 <= status < 500 and status != 429:
                    logger.warning("Discord rejected %s (HTTP %s); giving"
                                   " up on this message", label, status)
                    return False
                raise
        return True

    async def print_missed(self, row) -> int:
        async with self._print_locks.setdefault(row.id, asyncio.Lock()):
            try:
                thread = await self._ensure_thread(row)
            except discord.NotFound:
                # linked thread was deleted (404 Unknown Channel): rendering
                # waits for a RelinkButton; retrying is right, traceback
                # spam every tick is not
                logger.debug("thread gone for session %s; skipping missed "
                             "print until relink", row.id)
                return 0
            if thread is None:
                logger.warning("no thread for session %s; skipping missed "
                               "print", row.id)
                return 0
            printed = 0
            for msg in await self.hub.messages(row.id, unposted=True):
                if not await self._send_lines(
                        thread, [(msg.role, msg.content)],
                        f"session {row.id} message {msg.id}"):
                    await self.hub.mark_posted(row.id, [msg.id])  # poison
                    continue
                await self.hub.mark_posted(row.id, [msg.id])
                printed += 1
            return printed

    async def reprint_transcript(self, row) -> int:
        """Fresh thread bound to an old session: post the whole transcript
        (both roles), then mark everything rendered."""
        thread = await self._ensure_thread(row)
        if thread is None:
            return 0
        msgs = await self.hub.messages(row.id)
        await thread.send(
            embed=embeds.session_card(
                row, online=await self.agent_online(row.agent_id),
                messages=len(msgs)),
            view=views.session_card_view(row.id, None))
        for msg in msgs:
            if not await self._send_lines(
                    thread, [(msg.role, msg.content)],
                    f"session {row.id} message {msg.id}"):
                # _send_lines logged the HTTP status; give up like
                # print_missed does — still mark rendered
                logger.warning("gave up reprinting session %s message %s;"
                               " marked posted", row.id, msg.id)
        await self.hub.mark_posted(row.id, [m.id for m in msgs])
        return len(msgs)

    async def on_sync_applied(self, agent_id: str,
                              session_ids: list[str]) -> None:
        task = asyncio.create_task(self._print_missed_all(session_ids))
        # strong ref: the event loop keeps only weak ones, an unreferenced
        # task can be garbage-collected mid-flight
        self._sync_tasks.add(task)
        task.add_done_callback(self._sync_task_done)

    async def _print_missed_all(self, session_ids: list[str]) -> None:
        for sid in session_ids:
            try:
                row = await self.hub.get_session(sid)
                await self.print_missed(row)
            except NotFound:
                continue
            except Exception:
                logger.warning("missed print failed for %s", sid,
                               exc_info=True)

    def _sync_task_done(self, task: asyncio.Task) -> None:
        self._sync_tasks.discard(task)
        if not task.cancelled() and task.exception():
            logger.warning("sync print task failed",
                           exc_info=task.exception())

    async def print_missed_everywhere(self, deadline: float = 8.0) -> tuple[int, int]:
        """Wait (bounded) for adapter pushes to land, then render every
        missed message. Returns (messages printed, threads touched)."""
        loop_deadline = time.monotonic() + deadline
        while time.monotonic() < loop_deadline:
            if not await self.hub.missed_session_ids():
                break
            await asyncio.sleep(1.0)
        printed = threads = 0
        for sid in await self.hub.missed_session_ids():
            try:
                row = await self.hub.get_session(sid)
            except NotFound:
                continue
            n = await self.print_missed(row)
            if n:
                printed += n
                threads += 1
        return printed, threads


    def _check_owner(self, interaction) -> bool:
        return ensure_owner(interaction, self.settings.discord_owner_id)

    async def _reject(self, interaction, text: str) -> None:
        await interaction.response.send_message(text, ephemeral=True)

    # --- inbound messages --------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not isinstance(message.channel, discord.Thread):
            return
        if message.author.id != self.settings.discord_owner_id:
            return
        row = await self._session_for_thread(message.channel.id)
        if row is None:
            return
        try:
            await self._start_typing(row.id)
            prompt = await self._compose_prompt(message)
            if row.archived:  # posting re-opens the thread; keep the DB honest
                await self.hub.set_archived(row.id, False)
            await self.hub.send_message(row.id, prompt)
        except (AgentOffline, RunInProgress, NotFound) as exc:
            await self._finish_request(row.id)
            await message.channel.send(f"not sent: {exc}")

    async def _compose_prompt(self, message) -> str:
        author = content = None
        ref = message.reference
        if ref is not None and ref.message_id:
            orig = ref.resolved
            if orig is None:
                try:
                    orig = await message.channel.fetch_message(ref.message_id)
                except discord.HTTPException:
                    orig = None
            if orig is not None:
                author = orig.author.display_name
                content = orig.content
        return compose_prompt(message.content, author, content)

    # --- slash commands ----------------------------------------------------

    async def _resolve_session(self, interaction, session_id: str | None):
        if session_id:
            try:
                return await self.hub.get_session(session_id)
            except NotFound:
                return None
        if isinstance(interaction.channel, discord.Thread):
            return await self._session_for_thread(interaction.channel.id)
        return None

    def _thread_url(self, row, interaction) -> str | None:
        if row.thread_id is None or interaction.guild_id is None:
            return None
        return f"https://discord.com/channels/{interaction.guild_id}/{row.thread_id}"

    async def session_autocomplete(self, interaction, current: str):
        """session_id suggestions: non-archived, recent-first, title/id match."""
        rows = [r for r in await self.hub.list_sessions() if not r.archived]
        rows.sort(key=lambda r: r.updated_at or datetime.min, reverse=True)
        cur = current.lower()
        matches = [r for r in rows
                   if cur in r.id.lower() or cur in (r.title or "").lower()]
        return [app_commands.Choice(name=f"{display_title(r)[:90]} ({r.id})",
                                    value=r.id) for r in matches[:25]]

    async def agent_autocomplete(self, interaction, current: str):
        """agent suggestions: online agents only, id/name match."""
        rows = [r for r in await self.hub.agents() if r.online]
        cur = current.lower()
        matches = [r for r in rows
                   if cur in r.id.lower() or cur in (r.name or "").lower()]
        return [app_commands.Choice(name=f"{r.name or r.id} ({r.id})",
                                    value=r.id) for r in matches[:25]]

    async def _cmd_new(self, interaction, title: str | None,
                       workspace: str | None, agent: str | None = None) -> None:
        if not self._check_owner(interaction):
            await self._reject(interaction, "not authorized")
            return
        online = [row.id for row in await self.hub.agents() if row.online]
        if agent:
            if agent not in online:
                await self._reject(interaction,
                                   f"agent {agent} is offline or unknown")
                return
            agent_id = agent
        else:
            if not online:
                await self._reject(interaction, "no agent is online")
                return
            agent_id = online[0]
        # ack inside the 3s window: session create + thread setup routinely
        # overshoot it (hub PATCH + Discord REST), and an unacked token 404s
        # the final followup with 10015 ("The application did not respond")
        await interaction.response.defer(thinking=True)
        try:
            row = await self.hub.open_session(agent_id, str(interaction.user.id),
                                              workspace, title)
        except Exception as exc:  # AgentFailed / AgentOffline / hub errors
            await interaction.followup.send(f"session create failed: {exc}",
                                            ephemeral=True)
            return
        channel = interaction.channel
        thread = await channel.create_thread(
            name=display_title(row)[:100],
            type=discord.ChannelType.public_thread)
        await self.hub.attach_thread(row.id, thread.id)
        row.thread_id = thread.id
        await thread.send(
            embed=embeds.session_card(row, online=True, messages=0),
            view=views.session_card_view(row.id, None))
        await interaction.followup.send(
            embed=embeds.jump_card(row),
            view=views.thread_link_view(thread.id, interaction.guild_id))

    async def _cmd_sessions(self, interaction) -> None:
        """Private session picker (select menu + pagination) for /session
        list and /resume."""
        if not self._check_owner(interaction):
            await self._reject(interaction, "not authorized")
            return
        rows = [r for r in await self.hub.list_sessions() if not r.archived]
        rows.sort(key=lambda r: r.updated_at or datetime.min, reverse=True)
        if not rows:
            await self._reject(interaction, "(no sessions yet)")
            return
        picker = views.SessionPickerView(self, rows, interaction.guild_id)
        await interaction.response.send_message(embed=picker.embed(),
                                                view=picker, ephemeral=True)

    async def _cmd_info(self, interaction, session_id: str | None) -> None:
        if not self._check_owner(interaction):
            await self._reject(interaction, "not authorized")
            return
        row = await self._resolve_session(interaction, session_id)
        if row is None:
            await self._reject(interaction, "no session here; pass a session_id")
            return
        card = embeds.session_card(
            row, online=await self.agent_online(row.agent_id),
            messages=await self.hub.message_count(row.id))
        url = self._thread_url(row, interaction)
        if url is not None and await views.thread_deleted(self, row):
            url = None  # thread deleted: never serve a dead jump link
        await interaction.response.send_message(
            embed=card,
            view=views.session_card_view(row.id, url),
            ephemeral=True)

    async def _cmd_rename(self, interaction, title: str) -> None:
        if not self._check_owner(interaction):
            await self._reject(interaction, "not authorized")
            return
        row = await self._resolve_session(interaction, None)
        if row is None:
            await self._reject(interaction, "no session in this thread")
            return
        title = title.strip()[:100]
        if not title:
            await self._reject(interaction, "title must not be empty")
            return
        row = await self.hub.rename(row.id, title)
        thread = await self._thread_row(row)
        if thread is not None:
            try:
                await thread.edit(name=title)
            except Exception:
                logger.warning("thread rename failed for session %s", row.id,
                               exc_info=True)
        await interaction.response.send_message(f"renamed to {title}",
                                                ephemeral=True)

    async def _cmd_archive(self, interaction) -> None:
        if not self._check_owner(interaction):
            await self._reject(interaction, "not authorized")
            return
        row = await self._resolve_session(interaction, None)
        if row is None:
            await self._reject(interaction, "no session in this thread")
            return
        await self.hub.set_archived(row.id, True)
        thread = await self._thread_row(row)
        if thread is not None:
            try:
                await thread.edit(archived=True)
            except Exception:
                logger.warning("thread archive failed for session %s", row.id,
                               exc_info=True)
        await interaction.response.send_message("session archived",
                                                ephemeral=True)

    async def _cmd_delete(self, interaction, session_id: str | None) -> None:
        if not self._check_owner(interaction):
            await self._reject(interaction, "not authorized")
            return
        row = await self._resolve_session(interaction, session_id)
        if row is None:
            await self._reject(interaction, "no session here; pass a session_id")
            return
        try:
            await self.hub.delete_session(row.id)
        except RunInProgress:
            await self._reject(interaction,
                               "stop the run before deleting this session")
            return
        await interaction.response.send_message(
            f"🗑️ deleted `{row.id}` (thread kept)", ephemeral=True)

    async def _cmd_run_status(self, interaction, session_id: str | None) -> None:
        if not self._check_owner(interaction):
            await self._reject(interaction, "not authorized")
            return
        row = await self._resolve_session(interaction, session_id)
        if row is None:
            await self._reject(interaction, "no session here; pass a session_id")
            return
        status = await self.hub.run_status(row.id)
        lines = [f"session `{row.id}` · {row.status}",
                 f"run: {'in flight' if status.inflight else 'idle'}"]
        label = status.activity
        if label:
            lines.append(f"activity: {icons.activity_icon(label)} {label}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def _cmd_stop(self, interaction) -> None:
        if not self._check_owner(interaction):
            await self._reject(interaction, "not authorized")
            return
        row = await self._session_for_thread(interaction.channel.id)
        if row is None or not await self.hub.cancel(row.id):
            await self._reject(interaction, "nothing to stop")
            return
        await interaction.response.send_message("stopping…", ephemeral=True)

    async def _cmd_help(self, interaction) -> None:
        if not self._check_owner(interaction):
            await self._reject(interaction, "not authorized")
            return
        await interaction.response.send_message(embed=embeds.help_embed(),
                                                ephemeral=True)

    async def _cmd_agents(self, interaction) -> None:
        if not self._check_owner(interaction):
            await self._reject(interaction, "not authorized")
            return
        rows = await self.hub.agents()
        await interaction.response.send_message(
            embed=embeds.agent_status_embed(rows), ephemeral=True)


    async def _cmd_open(self, interaction, session_id: str) -> None:
        if not self._check_owner(interaction):
            await self._reject(interaction, "not authorized")
            return
        try:
            row = await self.hub.get_session(session_id)
        except NotFound:
            await self._reject(interaction, "unknown session")
            return
        await interaction.response.defer(thinking=True)
        try:
            channel = interaction.channel
            thread = await channel.create_thread(
                name=display_title(row)[:100],
                type=discord.ChannelType.public_thread)
            await self.hub.attach_thread(row.id, thread.id)
            row.thread_id = thread.id
            await self.reprint_transcript(row)
        except Exception as exc:
            # a hung deferred interaction must never dangle: report it
            await interaction.followup.send(
                f"session open failed: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            embed=embeds.jump_card(row),
            view=views.thread_link_view(thread.id, interaction.guild_id))

    async def _cmd_token_new(self, interaction, agent_id: str,
                             ttl_days: int | None) -> None:
        if not self._check_owner(interaction):
            await self._reject(interaction, "not authorized")
            return
        if ttl_days is not None and ttl_days <= 0:
            await self._reject(interaction, "ttl_days must be positive")
            return
        ttl = ttl_days or self.settings.token_ttl_days
        row, secret = await self.hub.mint_token(agent_id, ttl)
        await interaction.response.send_message(
            f"Token `{row.token_id}` minted for `{agent_id}`, expires "
            f"{row.expires_at:%Y-%m-%d %H:%M} UTC.\n\n"
            f"```{secret}```\n"
            "Shown once — store it now. Rotate with `/agent token rotate`.",
            ephemeral=True)

    async def _cmd_token_rotate(self, interaction, agent_id: str) -> None:
        if not self._check_owner(interaction):
            await self._reject(interaction, "not authorized")
            return
        row, secret = await self.hub.rotate_token(agent_id)
        await interaction.response.send_message(
            f"Rotated `{agent_id}`: previous token(s) revoked, new token "
            f"`{row.token_id}` expires {row.expires_at:%Y-%m-%d %H:%M} UTC."
            f"\n\n```{secret}```\nShown once — update the adapter's .env.",
            ephemeral=True)

    async def _cmd_token_revoke(self, interaction, agent_id: str) -> None:
        if not self._check_owner(interaction):
            await self._reject(interaction, "not authorized")
            return
        n = await self.hub.revoke_token(agent_id)
        await interaction.response.send_message(
            f"revoked {n} token(s) for `{agent_id}`; the agent can no "
            "longer connect until a new token is minted", ephemeral=True)

    async def _cmd_token_list(self, interaction) -> None:
        if not self._check_owner(interaction):
            await self._reject(interaction, "not authorized")
            return
        rows = await self.hub.list_tokens()
        if not rows:
            await self._reject(interaction, "(no tokens)")
            return
        lines = []
        for r in rows:
            who = r.agent_id or "bootstrap"
            state = "revoked" if r.revoked else \
                (f"expires {r.expires_at:%Y-%m-%d}" if r.expires_at
                 else "no expiry")
            lines.append(f"`{r.token_id}` · {who} · {state}")
        await interaction.response.send_message("\n".join(lines),
                                                ephemeral=True)

    async def _cmd_sync(self, interaction) -> None:
        if not self._check_owner(interaction):
            await self._reject(interaction, "not authorized")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            nudged = await self.hub.sync_nudge()
        except Exception:
            logger.warning("sync nudge failed", exc_info=True)
            nudged = 0
        printed, threads = await self.print_missed_everywhere()
        await interaction.followup.send(
            f"sync: nudged {nudged} agent(s); printed {printed} message(s) "
            f"in {threads} thread(s)", ephemeral=True)

def build_bot(settings, hub) -> SessionBot:
    bot = SessionBot(settings, hub)

    session_grp = app_commands.Group(name="session",
                                     description="Manage agent sessions")
    run_grp = app_commands.Group(name="run",
                                 description="Control the run in this thread")
    agent_grp = app_commands.Group(name="agent", description="Agent status")

    @session_grp.command(name="new", description="Start a session with an agent")
    @app_commands.describe(title="Short name for the session thread",
                           workspace="Optional workspace directory on the agent machine",
                           agent="Target agent (defaults to the first online agent)")
    @app_commands.autocomplete(agent=bot.agent_autocomplete)
    async def new_cmd(interaction: discord.Interaction, title: str | None = None,
                      workspace: str | None = None, agent: str | None = None):
        await bot._cmd_new(interaction, title, workspace, agent)

    @session_grp.command(name="list", description="Session picker (recent first)")
    async def list_cmd(interaction: discord.Interaction):
        await bot._cmd_sessions(interaction)

    @bot.tree.command(name="resume", description="Pick a recent session to continue")
    async def resume_cmd(interaction: discord.Interaction):
        await bot._cmd_sessions(interaction)


    @session_grp.command(name="open",
                         description="Open an old session in a new thread"
                                     " (reprints the transcript)")
    @app_commands.describe(session_id="Session to open")
    @app_commands.autocomplete(session_id=bot.session_autocomplete)
    async def open_cmd(interaction: discord.Interaction, session_id: str):
        await bot._cmd_open(interaction, session_id)

    @bot.tree.command(name="sync",
                      description="Sync missed messages now (all threads)")
    async def sync_cmd(interaction: discord.Interaction):
        await bot._cmd_sync(interaction)

    @session_grp.command(name="info", description="Show session details")
    @app_commands.describe(session_id="Session id (defaults to this thread's session)")
    @app_commands.autocomplete(session_id=bot.session_autocomplete)
    async def info_cmd(interaction: discord.Interaction, session_id: str | None = None):
        await bot._cmd_info(interaction, session_id)

    @session_grp.command(name="rename", description="Rename this thread's session")
    @app_commands.describe(title="New session title (the thread is renamed too)")
    async def rename_cmd(interaction: discord.Interaction, title: str):
        await bot._cmd_rename(interaction, title)

    @session_grp.command(name="archive", description="Archive this thread's session")
    async def archive_cmd(interaction: discord.Interaction):
        await bot._cmd_archive(interaction)

    @session_grp.command(name="delete",
                         description="Delete a session and its transcript (thread kept)")
    @app_commands.describe(session_id="Session id (defaults to this thread's session)")
    @app_commands.autocomplete(session_id=bot.session_autocomplete)
    async def delete_cmd(interaction: discord.Interaction,
                         session_id: str | None = None):
        await bot._cmd_delete(interaction, session_id)

    @run_grp.command(name="status", description="Show run status for this thread")
    @app_commands.describe(session_id="Session id (defaults to this thread's session)")

    @app_commands.autocomplete(session_id=bot.session_autocomplete)
    async def run_status_cmd(interaction: discord.Interaction,
                             session_id: str | None = None):
        await bot._cmd_run_status(interaction, session_id)

    @run_grp.command(name="cancel", description="Stop the in-flight run in this thread")
    async def cancel_cmd(interaction: discord.Interaction):
        await bot._cmd_stop(interaction)

    @agent_grp.command(name="status", description="List agents and capabilities")
    async def agent_status_cmd(interaction: discord.Interaction):
        await bot._cmd_agents(interaction)

    @bot.tree.command(name="help", description="Show available commands")
    async def help_cmd(interaction: discord.Interaction):
        await bot._cmd_help(interaction)

    token_grp = app_commands.Group(name="token", parent=agent_grp,
                                   description="Manage agent auth tokens")

    @token_grp.command(name="new", description="Mint a token for an agent id")
    @app_commands.describe(agent_id="Agent id the token is pinned to",
                           ttl_days="Days until expiry (default: token_ttl_days)")
    async def token_new_cmd(interaction: discord.Interaction, agent_id: str,
                            ttl_days: int | None = None):
        await bot._cmd_token_new(interaction, agent_id, ttl_days)

    @token_grp.command(name="rotate",
                       description="Revoke active tokens, mint a replacement")
    @app_commands.describe(agent_id="Agent id whose tokens rotate")
    async def token_rotate_cmd(interaction: discord.Interaction,
                               agent_id: str):
        await bot._cmd_token_rotate(interaction, agent_id)

    @token_grp.command(name="revoke",
                       description="Revoke an agent's tokens (agent goes offline)")
    @app_commands.describe(agent_id="Agent id whose tokens are revoked")
    async def token_revoke_cmd(interaction: discord.Interaction,
                               agent_id: str):
        await bot._cmd_token_revoke(interaction, agent_id)

    @token_grp.command(name="list", description="List tokens and their state")
    async def token_list_cmd(interaction: discord.Interaction):
        await bot._cmd_token_list(interaction)
    for grp in (session_grp, run_grp, agent_grp):
        bot.tree.add_command(grp)
    return bot
