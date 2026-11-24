from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from vivarium import places


@pytest.fixture(autouse=True)
def private_places(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """No test touches the user's own runs and environments. Short, under
    the temporary directory: a socket path has 104 bytes on macOS."""
    root = Path(tempfile.mkdtemp(prefix="viv-"))
    monkeypatch.setenv(places.STATE_ENV, str(root / "state"))
    monkeypatch.setenv(places.RUNTIME_ENV, str(root / "run"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def anyio_backend() -> str:
    """asyncio only. The mechanism underneath uses `loop.add_reader` and
    an asyncio subprocess, so trio is not a backend this can run on."""
    return "asyncio"
