from app.protocol import (
    AGENT_HEARTBEAT, AGENT_MESSAGE, AGENT_REGISTER, PROTO,
    SESSION_ACTIVITY, SESSION_DELTA,
    Envelope, new_id,
)


def test_new_id_formats():
    assert new_id("sess").startswith("sess_")
    assert len(new_id("req")) == 4 + 12


def test_envelope_roundtrip():
    env = Envelope(type=AGENT_REGISTER, payload={"agent_id": "omp-local"})
    raw = env.model_dump_json()
    again = Envelope.model_validate_json(raw)
    assert again == env
    assert again.protocol_version == 1
    assert again.payload == {"agent_id": "omp-local"}


def test_type_constants_are_namespaced():
    assert AGENT_REGISTER == "agent.register"
    assert AGENT_HEARTBEAT == "agent.heartbeat"
    assert AGENT_MESSAGE == "agent.message"
    assert PROTO == 1


def test_streaming_types_are_namespaced():
    assert SESSION_DELTA == "session.delta"
    assert SESSION_ACTIVITY == "session.activity"


def test_sync_types_mirrored_in_adapter():
    """The adapter's protocol.py is a verbatim mirror: its sync constants
    must equal the manager's (JSON envelope contract)."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent
    while not (root / "agent" / "local-adapter").is_dir():
        if root == root.parent:
            raise AssertionError("repo root with agent/local-adapter not found")
        root = root.parent
    adapter = (root / "agent" / "local-adapter" / "omp_adapter" / "protocol.py").read_text()
    from app import protocol as mgr
    for name in ("SYNC_PUSH", "SYNC_ACK", "SYNC_NOW", "SYNC_NEED",
                 "SYNC_MESSAGES"):
        assert f'{name} = "{getattr(mgr, name)}"' in adapter
