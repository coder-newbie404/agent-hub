"""Container entry: hub client + Discord bot (blocks forever)."""
import asyncio
import logging

from discord_frontend.config import Settings
from discord_frontend.hub import HubClient

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    settings = Settings()
    if not settings.discord_bot_token:
        raise SystemExit("DISCORD_BOT_TOKEN is required")
    if not settings.hub_token:
        raise SystemExit("HUB_TOKEN is required")
    hub = HubClient(settings.hub_url, settings.hub_token,
                    retry_delay=settings.retry_delay,
                    retry_cap=settings.retry_cap)
    from discord_frontend.bot import build_bot
    bot = build_bot(settings, hub)
    await bot.start(settings.discord_bot_token)


if __name__ == "__main__":
    asyncio.run(main())
