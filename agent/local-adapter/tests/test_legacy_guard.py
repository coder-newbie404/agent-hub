"""The old omp-adapter data dir must not be orphaned silently."""
import pytest

from agent_adapter.config import Settings, legacy_path_error


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ADAPTER_CONFIG", "")
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    return tmp_path


def _settings(**kw) -> Settings:
    return Settings(manager_url="wss://x", agent_token="t",
                    default_workspace="/tmp", runtime="omp", _env_file=None,
                    **kw)


def test_refuses_when_legacy_dir_exists_and_new_one_does_not(home):
    (home / ".omp-adapter").mkdir()
    msg = legacy_path_error(_settings(agent_id="work-omp"))
    assert msg is not None
    assert "work-omp" in msg
    target = home / ".agent-adapter" / "work-omp"
    assert (f"mkdir -p ~/.agent-adapter && mv ~/.omp-adapter {target}"
            in msg)


def test_passes_when_new_dir_already_exists(home):
    (home / ".omp-adapter").mkdir()
    (home / ".agent-adapter" / "omp-local").mkdir(parents=True)
    assert legacy_path_error(_settings()) is None


def test_passes_without_legacy_dir(home):
    assert legacy_path_error(_settings()) is None


def test_explicit_journal_dir_is_not_guarded(home):
    (home / ".omp-adapter").mkdir()
    assert legacy_path_error(_settings(journal_dir=str(home / "j"))) is None
