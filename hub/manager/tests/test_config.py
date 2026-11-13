"""Settings sanity (Discord id parsing moved to the frontend component)."""
from app.config import Settings


def test_offline_after_default_is_positive_int():
    """sweep_loop reads settings.offline_after every heartbeat — a missing
    field crashes the loop at runtime, not at boot (deploy bug 2026-09-15)."""
    s = Settings(admin_token="t")
    assert isinstance(s.offline_after, int) and s.offline_after > 0
