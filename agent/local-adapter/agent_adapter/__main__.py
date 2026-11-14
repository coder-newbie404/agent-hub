import asyncio
import logging
import time

import websockets
from pydantic import ValidationError

from agent_adapter.agent import AdapterAgent
from agent_adapter.client import AdapterClient
from agent_adapter.config import Settings, legacy_path_error

logging.basicConfig(level=logging.INFO)  # reconnect attempts must be visible
logger = logging.getLogger("agent_adapter")


def _config_error(exc: ValidationError) -> str:
    lines = ["invalid adapter configuration:"]
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"]) or "settings"
        msg = err["msg"].removeprefix("Value error, ")
        lines.append(f"  {field}: {msg}")
    return "\n".join(lines)


async def main() -> None:
    try:
        settings = Settings()
    except ValidationError as exc:
        # a config error is not a crash: the supervisor must not retry it
        raise SystemExit(_config_error(exc)) from None
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
