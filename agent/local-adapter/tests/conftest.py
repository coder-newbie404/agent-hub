import pathlib
import stat

import pytest

STUBS = pathlib.Path(__file__).parent / "stubs"


@pytest.fixture(scope="session", autouse=True)
def executable_stubs():
    """The exec bit does not survive every checkout (core.fileMode=false
    filesystems, zip exports); stubs are spawned directly, so enforce it."""
    for stub in STUBS.glob("*.sh"):
        stub.chmod(stub.stat().st_mode | stat.S_IRWXU | stat.S_IRGRP
                   | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)


@pytest.fixture(autouse=True)
def isolated_home(request, tmp_path_factory, monkeypatch):
    """No test may touch the real home: default journal/delegation paths
    derive from ~ and creating them there would also defeat the legacy-journal
    guard. test_real_binaries needs the real HOME for the CLIs' stored login."""
    if request.node.fspath.basename == "test_real_binaries.py":
        return
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
