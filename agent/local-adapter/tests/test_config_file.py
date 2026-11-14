"""Component config file (adapter.config.toml) vs env precedence."""
import os

import pytest
from pydantic import ValidationError

from agent_adapter.config import Settings


def _settings(tmp_path, toml: str, **env) -> Settings:
    cfg = tmp_path / "adapter.config.toml"
    cfg.write_text(toml)
    required = {"MANAGER_URL": "wss://h/ws/agent", "AGENT_TOKEN": "t",
                "DEFAULT_WORKSPACE": "/w", "RUNTIME": "omp"}
    merged = {**required, **env}
    old = {k: os.environ.pop(k, None) for k in merged}
    os.environ.update(merged)
    os.environ["ADAPTER_CONFIG"] = str(cfg)
    try:
        return Settings(_env_file=os.devnull)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.environ.pop("ADAPTER_CONFIG", None)


def test_toml_overrides_defaults(tmp_path):
    s = _settings(tmp_path, "sync_interval = 5\nagent_name = 'Worker'\n")
    assert s.sync_interval == 5
    assert s.agent_name == "Worker"
    assert s.heartbeat_interval == 30  # untouched default


def test_env_beats_toml(tmp_path):
    s = _settings(tmp_path, "sync_interval = 5\n",
                  SYNC_INTERVAL="9", MANAGER_URL="wss://h/ws/agent",
                  AGENT_TOKEN="t", DEFAULT_WORKSPACE="/w")
    assert s.sync_interval == 9


def test_important_fields_stay_env_only(tmp_path):
    s = _settings(tmp_path, "agent_token = 'from-config'\n",
                  MANAGER_URL="wss://h/ws/agent", AGENT_TOKEN="from-env",
                  DEFAULT_WORKSPACE="/w")
    assert s.agent_token == "from-env"
    assert s.manager_url == "wss://h/ws/agent"


def test_empty_allowed_root_is_none(tmp_path):
    s = _settings(tmp_path, "", ALLOWED_ROOT="")
    assert s.allowed_root is None


def test_tuning_keys_come_from_toml_and_derive_from_runtime(tmp_path):
    s = _settings(tmp_path, "policy = 'readonly'\n"
                            "runtime_args = ['--x']\n"
                            "env_passthrough = ['PROXY_*']\n",
                  RUNTIME="omp", AGENT_ID="", JOURNAL_DIR="")
    assert (s.policy, s.runtime_args, s.env_passthrough) == (
        "readonly", ["--x"], ["PROXY_*"])
    assert s.agent_id == "omp-local"
    assert s.journal_dir == "~/.agent-adapter/omp-local/journal"


def test_policy_from_toml_is_validated_against_the_runtime(tmp_path):
    with pytest.raises(ValidationError, match="supports policies"):
        _settings(tmp_path, "policy = 'yolo'\n")
