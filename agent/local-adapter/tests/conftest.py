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
