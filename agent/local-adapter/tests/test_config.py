"""Settings defaults for delegation/registration metadata (hermetic:
process env and .env must not leak into the asserted defaults)."""
import os

from agent_adapter.config import Settings


def _settings() -> Settings:
    env_keys = ["AGENT_DESCRIPTION", "AGENT_TOOLS", "AGENT_ENVIRONMENT",
                "DELEGATION_ENABLED", "DELEGATION_DIR"]
    old = {k: os.environ.pop(k, None) for k in env_keys}
    try:
        return Settings(manager_url="wss://x", agent_token="t",
                        default_workspace="/tmp", _env_file=os.devnull)
    finally:
        for k, v in old.items():
            if v is not None:
                os.environ[k] = v


def test_delegation_and_registration_defaults():
    s = _settings()
    assert s.agent_description == ""
    assert s.agent_tools == "[]"
    assert s.agent_environment == "{}"
    assert s.delegation_enabled is True
    assert s.delegation_dir.endswith("delegations")
