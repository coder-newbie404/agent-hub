import asyncio
import logging
import sys
import time
import tomllib

import websockets
from pydantic import ValidationError
from pydantic_settings import SettingsError

from agent_adapter.agent import AdapterAgent
from agent_adapter.client import AdapterClient
from agent_adapter.config import Settings, legacy_path_error
from agent_adapter.runtimes import names

logging.basicConfig(level=logging.INFO)  # reconnect attempts must be visible
logger = logging.getLogger("agent_adapter")

EX_CONFIG = 78  # sysexits.h; the unit lists it in RestartPreventExitStatus


def _refuse(message: str) -> None:
    """A config refusal is not a crash: the supervisor must not retry it."""
    print(message, file=sys.stderr)
    sys.exit(EX_CONFIG)


def _config_error(exc: Exception) -> str:
    """Field names and error text only: setting values may be secrets, so
    nothing from `input` is ever echoed."""
    lines = ["invalid adapter configuration:"]
    if isinstance(exc, ValidationError):
        for err in exc.errors():
            field = ".".join(str(p) for p in err["loc"]) or "settings"
            msg = err["msg"].removeprefix("Value error, ")
            if tuple(err["loc"]) == ("runtime",):
                msg += f" (available: {', '.join(names())})"
            lines.append(f"  {field}: {msg}")
    elif isinstance(exc, tomllib.TOMLDecodeError):
        lines.append(f"  config file is not valid TOML: {exc}")
    else:
        lines.append(f"  {exc}")
    return "\n".join(lines)


async def main() -> None:
    try:
        settings = Settings()
    except (ValidationError, SettingsError, tomllib.TOMLDecodeError) as exc:
        _refuse(_config_error(exc))
    msg = legacy_path_error(settings)
    if msg:
        _refuse(msg)

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
