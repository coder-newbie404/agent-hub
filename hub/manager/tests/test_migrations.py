"""The posted_at backfill is load-bearing: rows predating sync were all
live-posted, otherwise the first /sync would reprint history."""
import os
import sqlite3

from alembic import command
from alembic.config import Config


def _cfg(tmp_path):
    cfg = Config()
    p = tmp_path / "mig.db"
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg.set_main_option("script_location", os.path.join(here, "migrations"))
    os.environ["SQLITE_PATH"] = str(p)
    return cfg, p


def test_posted_at_backfill_marks_history_posted(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "mig.db"))
    cfg, p = _cfg(tmp_path)
    command.upgrade(cfg, "b3f1a9c2d4e5")
    con = sqlite3.connect(p)
    con.execute(
        "INSERT INTO sessions (id, agent_id, owner_id, status, created_at,"
        " updated_at) VALUES ('s1', 'a', 'o', 'completed', '2026-01-01"
        " 00:00:00.000000', '2026-01-01 00:00:00.000000')")
    con.execute(
        "INSERT INTO messages (session_id, role, content, created_at) VALUES"
        " ('s1', 'user', 'hello', '2026-01-01 00:01:00.000000')")
    con.commit()
    con.close()
    command.upgrade(cfg, "head")
    con = sqlite3.connect(p)
    posted, created = con.execute(
        "SELECT posted_at, created_at FROM messages").fetchone()
    assert posted == created  # backfilled: history counts as rendered
    con.close()


def test_sync_state_table_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "mig.db"))
    cfg, p = _cfg(tmp_path)
    command.upgrade(cfg, "head")
    con = sqlite3.connect(p)
    cols = {r[1] for r in con.execute("PRAGMA table_info(sync_state)")}
    assert {"agent_id", "instance", "cursor", "updated_at"} <= cols
    con.close()


def test_delegation_migration_owner_nullable_and_grants(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "mig.db"))
    cfg, p = _cfg(tmp_path)
    command.upgrade(cfg, "head")
    con = sqlite3.connect(p)
    # agent-delegated sessions carry no owner: NULL owner_id must insert
    con.execute(
        "INSERT INTO sessions (id, agent_id, owner_id, status, description,"
        " created_at, updated_at) VALUES ('s1', 'a', NULL, 'created', '',"
        " '2026-01-01 00:00:00.000000', '2026-01-01 00:00:00.000000')")
    cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
    assert {"creator_agent_id", "description", "last_used_at"} <= cols
    pk = [r[1] for r in sorted((r for r in con.execute(
        "PRAGMA table_info(session_grants)") if r[5]), key=lambda r: r[5])]
    assert pk == ["session_id", "agent_id"]
    con.commit()
    con.close()
