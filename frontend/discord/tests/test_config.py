"""Env parsing: empty string ids mean unset (ported from manager's
config tests — the parsing moved here with the Discord settings)."""
from discord_frontend.config import Settings


def test_empty_guild_id_is_none():
    s = Settings(discord_guild_id="")
    assert s.discord_guild_id is None


def test_guild_id_parses_int():
    s = Settings(discord_guild_id="123456789")
    assert s.discord_guild_id == 123456789


def test_empty_sync_channel_is_none():
    s = Settings(discord_sync_channel_id="")
    assert s.discord_sync_channel_id is None


def test_sync_channel_parses_int():
    s = Settings(discord_sync_channel_id="999")
    assert s.discord_sync_channel_id == 999


def test_stream_interval_default():
    s = Settings()
    assert s.stream_interval == 2.0
