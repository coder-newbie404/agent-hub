"""Adapter settings.

Split by secret-ness:
- env (`.env` / process env): important + secret values — MANAGER_URL,
  AGENT_TOKEN, DEFAULT_WORKSPACE, ALLOWED_ROOT.
- config file (config/adapter.config.toml, component-only): daemon tuning
  that is safe to commit. ADAPTER_CONFIG points at it; env wins over the
  file, the file wins over field defaults.
"""
import os

from pydantic import field_validator
from pydantic_settings import (BaseSettings, PydanticBaseSettingsSource,
                               SettingsConfigDict, TomlConfigSettingsSource)


class Settings(BaseSettings):
    # important & secret (env)
    manager_url: str
    agent_token: str
    default_workspace: str
    allowed_root: str | None = None

    # component tuning (config/adapter.config.toml; env overrides)
    agent_id: str = "omp-local"
    agent_name: str = "OMP Local"
    omp_bin: str = "omp"
    omp_titles_db: str = "~/.omp/agent/history.db"
    journal_dir: str = "~/.omp-adapter/journal"
    state_file: str = "~/.omp-adapter/state.json"
    heartbeat_interval: int = 30
    reconnect_max: int = 30
    sync_interval: int = 60
    sync_batch: int = 200
    stream_limit_bytes: int = 64 * 1024 * 1024
    agent_description: str = ""
    agent_tools: str = "[]"
    agent_environment: str = "{}"
    delegation_enabled: bool = True
    delegation_dir: str = "~/.omp-adapter/delegations"

    @field_validator("allowed_root", mode="before")
    @classmethod
    def _empty_is_none(cls, v):
        """An empty env var means 'unset', not a parse error."""
        if v == "" or v is None:
            return None
        return v

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [
            init_settings, env_settings]
        config_file = os.getenv("ADAPTER_CONFIG", "config/adapter.config.toml")
        if config_file and os.path.exists(os.path.expanduser(config_file)):
            sources.append(
                TomlConfigSettingsSource(settings_cls, toml_file=config_file))
        sources += [dotenv_settings, file_secret_settings]
        return tuple(sources)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
