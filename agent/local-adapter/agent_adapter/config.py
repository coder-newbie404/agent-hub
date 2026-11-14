"""Adapter settings.

Split by secret-ness:
- env (`.env` / process env): important + secret values — RUNTIME,
  MANAGER_URL, AGENT_TOKEN, DEFAULT_WORKSPACE, ALLOWED_ROOT.
- config file (config/adapter.config.toml, component-only): daemon tuning
  that is safe to commit. ADAPTER_CONFIG points at it; env wins over the
  file, the file wins over field defaults.
"""
import os
import pathlib

from pydantic import field_validator, model_validator
from pydantic_settings import (BaseSettings, PydanticBaseSettingsSource,
                               SettingsConfigDict, TomlConfigSettingsSource)

from agent_adapter.runtimes import get_runtime


class Settings(BaseSettings):
    # important & secret (env)
    manager_url: str
    agent_token: str
    default_workspace: str
    runtime: str
    allowed_root: str | None = None

    # component tuning (config/adapter.config.toml; env overrides)
    runtime_bin: str = ""           # "" = the runtime's own name on PATH
    policy: str = "default"
    model: str | None = None
    runtime_args: list[str] = []
    env_passthrough: list[str] = []  # fnmatch patterns of extra child env
    allow_payload_env: bool = False  # hub payload `env` reaches the child
    omp_titles_db: str = "~/.omp/agent/history.db"
    agent_id: str = ""
    agent_name: str = ""
    journal_dir: str = ""
    state_file: str = ""
    heartbeat_interval: int = 30
    reconnect_max: int = 30
    sync_interval: int = 60
    sync_batch: int = 200
    stream_limit_bytes: int = 64 * 1024 * 1024
    agent_description: str = ""
    agent_tools: str = "[]"
    agent_environment: str = "{}"
    delegation_enabled: bool = True
    delegation_dir: str = ""

    @field_validator("allowed_root", mode="before")
    @classmethod
    def _empty_is_none(cls, v):
        """An empty env var means 'unset', not a parse error."""
        if v == "" or v is None:
            return None
        return v

    @model_validator(mode="after")
    def _resolve(self):
        spec = get_runtime(self.runtime)       # raises UnknownRuntime(ValueError)
        if self.policy not in spec.policies:
            raise ValueError(
                f"runtime {self.runtime!r} supports policies "
                f"{sorted(spec.policies)}, not {self.policy!r}")
        if not self.agent_id:
            self.agent_id = f"{self.runtime}-local"
        if not self.agent_name:
            self.agent_name = f"{self.runtime} local"
        base = f"~/.agent-adapter/{self.agent_id}"
        self.journal_dir = self.journal_dir or f"{base}/journal"
        self.state_file = self.state_file or f"{base}/state.json"
        self.delegation_dir = self.delegation_dir or f"{base}/delegations"
        return self

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


def legacy_path_error(settings) -> str | None:
    """Refuse to start when an old omp-adapter data dir exists and the new
    default location does not: the journal is the source of truth and must
    not be orphaned silently. Only applies to the derived default location."""
    legacy = pathlib.Path("~/.omp-adapter").expanduser()
    if not settings.journal_dir.startswith("~/.agent-adapter/"):
        return None
    target = pathlib.Path(settings.journal_dir).expanduser().parent
    if legacy.exists() and not target.exists():
        return (f"found legacy adapter data in {legacy} but {target} does not "
                f"exist. To keep that journal for agent '{settings.agent_id}': "
                f"mkdir -p ~/.agent-adapter && mv ~/.omp-adapter {target}  "
                f"(or delete/rename ~/.omp-adapter to start fresh)")
    return None
