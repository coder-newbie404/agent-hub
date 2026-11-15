"""Adapter settings.

Split by secret-ness:
- env (process env, plus a `.env` in the working directory that the entrypoint
  loads into it without overriding): important + secret values — RUNTIME,
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
        sources += [file_secret_settings]
        return tuple(sources)

    model_config = SettingsConfigDict(extra="ignore")


def legacy_path_error(settings) -> str | None:
    """Refuse to start an omp instance when an old omp-adapter data dir
    exists and the new default location has no journal: the journal is the
    source of truth and must not be orphaned silently. Keyed on the journal,
    not the directory, so an empty dir created by a side effect never
    bypasses it. Only applies to the derived default location, and only to
    omp (the legacy data belongs to the omp adapter)."""
    if settings.runtime != "omp":
        return None
    legacy = pathlib.Path("~/.omp-adapter").expanduser()
    if not settings.journal_dir.startswith("~/.agent-adapter/"):
        return None
    journal = pathlib.Path(settings.journal_dir).expanduser()
    if legacy.exists() and not journal.exists():
        target = journal.parent
        return (f"found legacy adapter data in {legacy} but {journal} does "
                f"not exist. To keep that journal for agent "
                f"'{settings.agent_id}': mkdir -p ~/.agent-adapter && "
                f"mv ~/.omp-adapter {target}  "
                f"(or delete/rename ~/.omp-adapter to start fresh)")
    return None
