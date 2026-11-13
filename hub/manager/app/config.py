"""Manager settings.

Split by secret-ness:
- env (`.env` / process env): secrets and deployment-critical values —
  ADMIN_TOKEN, the database path.
- config file (config/manager.config.toml, component-only): tuning knobs
  that are safe to commit. MANAGER_CONFIG overrides the path; env vars
  always win over the file, the file wins over field defaults.
"""
import os

from pydantic_settings import (BaseSettings, PydanticBaseSettingsSource,
                               SettingsConfigDict, TomlConfigSettingsSource)


class Settings(BaseSettings):
    # secrets & deployment-critical (env)
    admin_token: str = ""  # bootstrap/admin credential: admin REST, /ws/ui, agent WS
    sqlite_path: str = "/data/manager.db"

    # component tuning (config/manager.config.toml; env overrides)
    token_ttl_days: int = 30
    heartbeat_interval: int = 30
    offline_after: int = 180  # sweep marks agents offline after this many silent seconds
    # Delegation broker: A's deadline for a delegated round, bookkeeping
    # ages for delegated sessions (idle/archive — used in Task 7), and
    # the hop cap that refuses prompt loops.
    delegation_result_timeout_s: float = 600.0
    delegation_idle_after_s: int = 21600       # 6 h
    delegation_archive_after_s: int = 1209600  # 14 d
    delegation_max_hop: int = 3

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
        # Default component file next to the checkout, overridable via
        # MANAGER_CONFIG. Existence-guarded so bare dev runs (no config
        # dir) stay quiet.
        config_file = os.getenv("MANAGER_CONFIG", "config/manager.config.toml")
        if config_file and os.path.exists(config_file):
            sources.append(
                TomlConfigSettingsSource(settings_cls, toml_file=config_file))
        sources += [dotenv_settings, file_secret_settings]
        return tuple(sources)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
