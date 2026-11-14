import asyncio
import logging
import time

import websockets

from agent_adapter.agent import AdapterAgent
from agent_adapter.client import AdapterClient
from agent_adapter.config import Settings, legacy_path_error

logging.basicConfig(level=logging.INFO)  # reconnect attempts must be visible
logger = logging.getLogger("agent_adapter")


async def main() -> None:
    settings = Settings()
    msg = legacy_path_error(settings)
    if msg:
        raise SystemExit(msg)

    async def send(env) -> None:
        ws = client.ws
        if ws is None:
            return  # hub offline: events are best-effort; snapshot reconciles
        try:
            await ws.send(env.model_dump_json())
        except websockets.ConnectionClosed:
            pass

    agent = AdapterAgent(settings, send)
    client = AdapterClient(settings, agent)
    await client.run_forever()


def run() -> None:
    """Daemon supervisor: run_forever is already retry-safe; this catches
    anything that would still kill the interpreter (env-level teardown,
    asyncio internals) so a WSL/PTY hiccup can't take the agent offline.
    Ctrl+C and a deliberate SystemExit (config refusal) stay clean exits."""
    while True:
        try:
            asyncio.run(main())
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            logger.exception("adapter crashed; restarting in 5s")
            time.sleep(5)


if __name__ == "__main__":
    run()
