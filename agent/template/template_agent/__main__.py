"""Run the template agent: `python -m template_agent`
Env: MANAGER_URL (default ws://127.0.0.1:8000/ws/agent), AGENT_TOKEN (required),
optional AGENT_ID, AGENT_NAME, AGENT_DESCRIPTION, REPLY."""
import asyncio
import logging

import websockets

from .agent import Agent, Settings
from .protocol import AGENT_HEARTBEAT, AGENT_REGISTER, Envelope

log = logging.getLogger(__name__)

HEARTBEAT_S = 30


async def serve(ws, agent: Agent, settings: Settings) -> None:
    """One hub connection: register, heartbeat, dispatch frames. However the
    connection ends, in-flight runs and the heartbeat are cancelled and
    reaped before returning (nothing outlives the socket it was bound to)."""
    await ws.send(Envelope(
        type=AGENT_REGISTER,
        payload={"agent_id": settings.agent_id,
                 "name": settings.agent_name,
                 "description": settings.agent_description,
                 "capabilities": ["chat", "cancel", "streaming"]},
    ).model_dump_json())
    log.info("registered as %s", settings.agent_id)

    async def heartbeat():
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            await ws.send(Envelope(type=AGENT_HEARTBEAT).model_dump_json())

    hb = asyncio.create_task(heartbeat())
    try:
        async for raw in ws:
            await agent.handle(Envelope.model_validate_json(raw), ws)
    finally:
        hb.cancel()
        try:
            # a heartbeat that already died (e.g. ConnectionClosed) is
            # retrieved here; it is expected on disconnect, not an error
            await asyncio.gather(hb, return_exceptions=True)
        finally:
            await agent.close()


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings(_env_file=None)  # env vars only
    agent = Agent(settings)
    attempt = 0
    while True:
        try:
            async with websockets.connect(
                settings.manager_url,
                additional_headers={"authorization":
                                    f"Bearer {settings.agent_token}"},
            ) as ws:
                attempt = 0
                await serve(ws, agent, settings)
        except (websockets.ConnectionClosed, websockets.InvalidHandshake,
                OSError) as exc:
            # InvalidHandshake covers InvalidMessage/InvalidStatus: a hub that
            # is restarting accepts TCP before it can answer HTTP. Connect
            # timeouts are TimeoutError, an OSError. Bad config (InvalidURI)
            # and bugs are deliberately not retried.
            delay = min(2 ** attempt, 30)
            attempt += 1
            log.warning("hub unreachable (%s); retry in %.0fs", exc, delay)
            await asyncio.sleep(delay)
        else:
            # clean close: pause so a hub that accepts then closes at once
            # is not hot-looped (attempt was reset on connect)
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
