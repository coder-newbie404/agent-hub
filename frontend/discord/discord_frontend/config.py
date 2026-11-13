"""Frontend settings (env-first, omp-adapter style).

Secrets and deployment values come from the environment (.env loaded by
docker-compose / systemd EnvironmentFile). All knobs the bot itself reads
live here now — the hub no longer knows about Discord."""
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # secrets & deployment-critical (env)
    hub_url: str = "http://manager:8000"
    hub_token: str = ""  # admin bearer for REST + /ws/ui
    discord_bot_token: str = ""
    discord_owner_id: int = 0
    discord_guild_id: int | None = None
    discord_sync_channel_id: int | None = None

    # tuning (env; nothing here is secret)
    token_ttl_days: int = 30  # /agent token rotate default
    stream_interval: float = 2.0  # live-preview edit throttle
    retry_delay: float = 1.0  # /ws/ui reconnect backoff start
    retry_cap: float = 60.0

    @field_validator("discord_guild_id", "discord_sync_channel_id",
                     mode="before")
    @classmethod
    def _empty_ids_are_none(cls, v):
        """An empty env var means 'unset', not a parse error."""
        if v == "" or v is None:
            return None
        return v

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
