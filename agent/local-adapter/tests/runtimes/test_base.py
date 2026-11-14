import pytest

from agent_adapter.runtimes import UnknownRuntime, get_runtime, names
from agent_adapter.runtimes.base import child_env

ENV = {
    "PATH": "/bin", "HOME": "/home/x", "LC_ALL": "C.UTF-8",
    "XDG_CONFIG_HOME": "/home/x/.config",
    "AGENT_TOKEN": "secret", "MANAGER_URL": "wss://x", "ALLOWED_ROOT": "/w",
    "FOO_API_KEY": "k", "MY_VAR": "v",
}


def _env(**kw):
    args = dict(auth=(), passthrough=[], exclude=set(), extra={})
    args.update(kw)
    return child_env(ENV, **args)


def test_child_env_keeps_base_and_drops_adapter_secrets():
    out = _env()
    assert {"PATH", "HOME", "LC_ALL", "XDG_CONFIG_HOME"} <= set(out)
    assert not {"AGENT_TOKEN", "MANAGER_URL", "ALLOWED_ROOT"} & set(out)


def test_child_env_auth_and_passthrough_patterns():
    assert "FOO_API_KEY" not in _env()
    assert _env(auth=("*_API_KEY",))["FOO_API_KEY"] == "k"
    assert "MY_VAR" not in _env()
    assert _env(passthrough=["MY_*"])["MY_VAR"] == "v"


def test_child_env_exclude_beats_patterns():
    out = _env(passthrough=["AGENT_*"], exclude={"AGENT_TOKEN"})
    assert "AGENT_TOKEN" not in out


def test_child_env_extra_overrides_and_adds():
    out = _env(extra={"PATH": "/x", "NEW": "1"})
    assert out["PATH"] == "/x" and out["NEW"] == "1"


def test_registry_names_and_lookup():
    assert names() == ["claude", "omp", "opencode"]
    assert get_runtime("omp").name == "omp"


def test_unknown_runtime_lists_available():
    with pytest.raises(UnknownRuntime) as e:
        get_runtime("nope")
    for n in names():
        assert n in str(e.value)
