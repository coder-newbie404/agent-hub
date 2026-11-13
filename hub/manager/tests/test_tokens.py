"""Per-agent token lifecycle: hashing, expiry, rotation, revocation,
bootstrap seeding — the contract a leaked token's damage depends on."""
from datetime import timedelta

import pytest

from app.models import AgentTokenRow, utcnow
from app.tokens import TokenService, bearer_secret, hash_secret


async def make_service() -> TokenService:
    import app.db as db
    engine = db.make_engine(":memory:")
    async with engine.begin() as conn:
        await conn.run_sync(AgentTokenRow.metadata.create_all)
    return TokenService(db.make_session_maker(engine))


async def test_issue_and_verify_roundtrip():
    ts = await make_service()
    row, secret = await ts.issue("omp-local")
    assert secret.startswith("ask_")
    found = await ts.verify(secret)
    assert found is not None and found.token_id == row.token_id
    assert found.agent_id == "omp-local"
    assert found.last_used_at is not None  # verify touches last_used
    assert await ts.verify("ask_bogus") is None
    assert await ts.verify("") is None


async def test_expired_token_rejected():
    ts = await make_service()
    row, secret = await ts.issue("omp-local", ttl_days=1)
    async with ts._maker() as s:
        r = await s.get(AgentTokenRow, row.token_id)
        r.expires_at = utcnow() - timedelta(seconds=1)
        await s.commit()
    assert await ts.verify(secret) is None


async def test_revoke_and_rotate():
    ts = await make_service()
    _, old = await ts.issue("omp-local")
    row2, new = await ts.rotate("omp-local")
    assert await ts.verify(old) is None, "rotation must kill the old secret"
    assert (await ts.verify(new)).token_id == row2.token_id
    # revoke kills every token of the agent, including a second one
    _, extra = await ts.issue("omp-local")
    assert await ts.revoke(agent_id="omp-local") >= 2
    assert await ts.verify(new) is None
    assert await ts.verify(extra) is None


async def test_revoke_leaves_other_agents_and_bootstrap():
    ts = await make_service()
    _, other = await ts.issue("other-agent")
    _, boot = await ts.issue(None)  # bootstrap/wildcard
    await ts.revoke(agent_id="omp-local")
    assert (await ts.verify(other)) is not None
    # agent revocation must not touch the bootstrap wildcard
    _, mine = await ts.issue("omp-local")
    await ts.revoke(agent_id="omp-local")
    assert (await ts.verify(boot)) is not None
    assert await ts.verify(mine) is None


async def test_bootstrap_restored_after_overbroad_revoke():
    ts = await make_service()
    await ts.ensure_bootstrap("admin-secret")
    # an operator revokes everything (old behaviour hit the wildcard too)
    async with ts._maker() as s:
        from sqlalchemy import update
        await s.execute(update(AgentTokenRow)
                        .values(revoked_at=utcnow()))
        await s.commit()
    assert await ts.verify("admin-secret") is None
    # boot re-seeds from env: the env-pinned secret is authoritative
    await ts.ensure_bootstrap("admin-secret")
    assert (await ts.verify("admin-secret")) is not None


async def test_issue_rejects_bad_ttl():
    ts = await make_service()
    with pytest.raises(ValueError):
        await ts.issue("omp-local", ttl_days=0)


async def test_bootstrap_seed_first_boot_and_rotation():
    ts = await make_service()
    assert await ts.ensure_bootstrap("admin-secret") is True
    rows = await ts.list()
    assert len(rows) == 1 and rows[0].agent_id is None
    assert (await ts.verify("admin-secret")) is not None
    # re-boot with the same secret: idempotent, still one row
    assert await ts.ensure_bootstrap("admin-secret") is False
    assert len(await ts.list()) == 1
    # env rotation: stale bootstrap revoked, new secret becomes the wildcard
    assert await ts.ensure_bootstrap("rotated-secret") is False
    assert await ts.verify("admin-secret") is None
    assert (await ts.verify("rotated-secret")) is not None


async def test_bootstrap_seed_noop_without_secret():
    ts = await make_service()
    assert await ts.ensure_bootstrap("") is False
    assert await ts.list() == []


async def test_ws_binding_pin_token_to_agent():
    """verify() hands back the pinned identity; ws.py relies on it."""
    ts = await make_service()
    await ts.issue("omp-local")
    row, secret = await ts.issue("omp-local")
    found = await ts.verify(secret)
    assert found.agent_id == "omp-local"


def test_bearer_secret_extracts_after_prefix():
    assert bearer_secret("Bearer ask_x") == "ask_x"
    assert bearer_secret("ask_x") == "ask_x"  # no prefix: raw
    assert bearer_secret("Bearer ask_Ø") == "ask_Ø"  # non-ASCII must not raise
    assert bearer_secret("") == ""


def test_hash_secret_is_total():
    # non-ASCII bytes must hash (replace) rather than raise TypeError
    assert hash_secret("tØken") == hash_secret("tØken")
