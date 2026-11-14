"""Settings defaults and runtime resolution (hermetic: process env, `.env` and
the component config file must not leak into the asserted values)."""
import pytest
from pydantic import ValidationError

from agent_adapter.config import Settings

OLD_BIN_VAR = "OMP_" + "BIN"  # the pre-rename setting; must stay inert


@pytest.fixture(autouse=True)
def hermetic_env(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.delenv(OLD_BIN_VAR, raising=False)
    monkeypatch.setenv("ADAPTER_CONFIG", "")  # no config file


def _settings(**kw) -> Settings:
    base = dict(manager_url="wss://x", agent_token="t",
                default_workspace="/tmp", runtime="omp", _env_file=None)
    return Settings(**{**base, **kw})


def test_delegation_and_registration_defaults():
    s = _settings()
    assert s.agent_description == ""
    assert s.agent_tools == "[]"
    assert s.agent_environment == "{}"
    assert s.delegation_enabled is True


def test_runtime_is_required():
    with pytest.raises(ValidationError, match="runtime"):
        Settings(manager_url="wss://x", agent_token="t",
                 default_workspace="/tmp", _env_file=None)


def test_unknown_runtime_lists_available_names():
    with pytest.raises(ValidationError) as ei:
        _settings(runtime="nope")
    msg = str(ei.value)
    assert "unknown runtime 'nope'" in msg
    for name in ("claude", "omp", "opencode"):
        assert name in msg


def test_unsupported_policy_is_refused():
    with pytest.raises(ValidationError, match="supports policies"):
        _settings(policy="bogus")


def test_agent_identity_defaults_follow_runtime():
    s = _settings()
    assert s.agent_id == "omp-local"
    assert s.agent_name == "omp local"
    s = _settings(agent_id="work-omp", agent_name="Work")
    assert (s.agent_id, s.agent_name) == ("work-omp", "Work")


def test_data_dirs_derive_from_agent_id():
    s = _settings(agent_id="work-omp")
    base = "~/.agent-adapter/work-omp"
    assert s.journal_dir == f"{base}/journal"
    assert s.state_file == f"{base}/state.json"
    assert s.delegation_dir == f"{base}/delegations"


def test_explicit_data_dirs_win():
    s = _settings(journal_dir="/j", state_file="/s.json", delegation_dir="/d")
    assert (s.journal_dir, s.state_file, s.delegation_dir) == (
        "/j", "/s.json", "/d")


def test_runtime_bin_is_a_setting_and_the_old_bin_var_is_ignored(monkeypatch):
    assert _settings().runtime_bin == ""
    monkeypatch.setenv(OLD_BIN_VAR, "/old/omp")
    assert _settings().runtime_bin == ""  # the old name is ignored
    monkeypatch.setenv("RUNTIME_BIN", "/new/omp")
    assert _settings().runtime_bin == "/new/omp"


def test_list_settings_come_from_json_env(monkeypatch):
    monkeypatch.setenv("RUNTIME_ARGS", '["--foo", "bar"]')
    monkeypatch.setenv("ENV_PASSTHROUGH", '["HTTP_*"]')
    s = _settings()
    assert s.runtime_args == ["--foo", "bar"]
    assert s.env_passthrough == ["HTTP_*"]
    assert s.allow_payload_env is False
