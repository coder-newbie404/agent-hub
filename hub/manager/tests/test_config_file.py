"""Component config file (manager.config.toml) vs env precedence."""
from app.config import Settings


def test_toml_overrides_defaults(monkeypatch, tmp_path):
    cfg = tmp_path / "manager.config.toml"
    cfg.write_text("offline_after = 42\n")
    monkeypatch.setenv("MANAGER_CONFIG", str(cfg))
    s = Settings(admin_token="t")
    assert s.offline_after == 42
    assert s.heartbeat_interval == 30  # untouched default


def test_env_beats_toml(monkeypatch, tmp_path):
    cfg = tmp_path / "manager.config.toml"
    cfg.write_text("offline_after = 42\n")
    monkeypatch.setenv("MANAGER_CONFIG", str(cfg))
    monkeypatch.setenv("OFFLINE_AFTER", "7")
    s = Settings(admin_token="t")
    assert s.offline_after == 7


def test_missing_config_file_is_quiet(monkeypatch):
    monkeypatch.setenv("MANAGER_CONFIG", "/nonexistent/manager.config.toml")
    s = Settings(admin_token="t")
    assert s.offline_after == 180


def test_repo_config_ships_defaults(monkeypatch):
    import os
    cfg = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                       "config", "manager.config.toml")
    monkeypatch.setenv("MANAGER_CONFIG", cfg)
    s = Settings(admin_token="t")
    assert s.token_ttl_days == 30
