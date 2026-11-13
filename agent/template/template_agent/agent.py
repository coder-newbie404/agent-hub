"""Minimal protocol-v1 agent behavior: session.create -> session.waiting ack;
one streamed canned reply per session.message; cancel -> cancel_ack (+
session.failed: cancelled). Every reply frame echoes the request_id of the
hub frame it answers (the hub correlates acks and transcript rows by it).
This module is the copy-me reference; local-adapter is the real one."""
import asyncio
import logging
import weakref
from contextlib import suppress

from pydantic_settings import BaseSettings
from websockets.exceptions import ConnectionClosed

from .protocol import (AGENT_MESSAGE, SESSION_CANCEL, SESSION_CANCEL_ACK,
                       SESSION_COMPLETED, SESSION_CREATE, SESSION_DELTA,
                       SESSION_FAILED, SESSION_WAITING, Envelope)

log = logging.getLogger(__name__)


class Settings(BaseSettings):
    manager_url: str = "ws://127.0.0.1:8000/ws/agent"
    agent_token: str
    agent_id: str = "template"
    agent_name: str = "Template Agent"
    agent_description: str = ""
    reply: str = "Hello from the template agent."


class Agent:
    def __init__(self, settings):
        self.settings = settings
        self._runs: dict[str | None, asyncio.Task] = {}
        # run tasks we cancelled on the hub's behalf (hub cancel / superseded).
        # Keyed per task, not per session: two runs of one session can
        # coexist briefly after a supersede, and each must see only its own
        # marker.
        self._hub_cancelled: weakref.WeakSet[asyncio.Task] = weakref.WeakSet()

    def _cancel_for_hub(self, task: asyncio.Task) -> None:
        self._hub_cancelled.add(task)
        task.cancel()

    async def close(self) -> None:
        """Connection is gone: cancel and reap every run, drop the registry."""
        tasks = list(self._runs.values())
        self._runs.clear()
        for task in tasks:
            task.cancel()
        # return_exceptions retrieves each task's outcome (e.g. a send that
        # died with ConnectionClosed) so nothing is logged as never retrieved
        await asyncio.gather(*tasks, return_exceptions=True)

    async def handle(self, env: Envelope, ws) -> None:
        if env.type == SESSION_CREATE:
            # The hub awaits a frame with the same session_id + request_id
            # (30 s timeout): session.waiting accepts, session.failed
            # {reason} refuses. The template has nothing to provision, so
            # it always accepts.
            await ws.send(Envelope(type=SESSION_WAITING,
                                   request_id=env.request_id,
                                   session_id=env.session_id,
                                   payload={"state": "created"}
                                   ).model_dump_json())
        elif env.type == "session.message":
            old = self._runs.get(env.session_id)
            if old and not old.done():
                self._cancel_for_hub(old)
            self._runs[env.session_id] = asyncio.create_task(
                self._run(env, ws))
        elif env.type == SESSION_CANCEL:
            task = self._runs.get(env.session_id)
            if task and not task.done():
                self._cancel_for_hub(task)
            # same session_id + request_id as the hub's cancel (acked
            # cancels are matched on both; legacy cancels carry no id)
            await ws.send(Envelope(type=SESSION_CANCEL_ACK,
                                   request_id=env.request_id,
                                   session_id=env.session_id,
                                   payload={"accepted": True}).model_dump_json())

    async def _run(self, env: Envelope, ws) -> None:
        # request_id: the hub stores it on the transcript row and ties the
        # run's frames back to the session.message that started it
        delta = Envelope(type=SESSION_DELTA, request_id=env.request_id,
                         session_id=env.session_id,
                         payload={"text": self.settings.reply[:20]})
        message = Envelope(type=AGENT_MESSAGE, request_id=env.request_id,
                           session_id=env.session_id,
                           payload={"text": self.settings.reply})
        completed = Envelope(type=SESSION_COMPLETED,
                             request_id=env.request_id,
                             session_id=env.session_id,
                             payload={"text": self.settings.reply})
        try:
            await ws.send(delta.model_dump_json())
            await ws.send(message.model_dump_json())
            await ws.send(completed.model_dump_json())
        except asyncio.CancelledError:
            # Only a hub-requested cancel reports the run as failed; any other
            # cancellation (connection teardown) just propagates, no frames.
            if asyncio.current_task() in self._hub_cancelled:
                with suppress(ConnectionClosed):
                    await ws.send(Envelope(type=SESSION_FAILED,
                                           request_id=env.request_id,
                                           session_id=env.session_id,
                                           payload={"reason": "cancelled"}
                                           ).model_dump_json())
            raise
