"""`unixpath.fits` against the kernel's own `bind`."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from vivarium_runner import unixpath


def binds(path: Path) -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(str(path))
        except OSError:
            return False
    path.unlink()
    return True


@pytest.mark.parametrize("extra", [-1, 0, 1])
def test_fits_agrees_with_bind_at_the_limit(tmp_path: Path, extra: int) -> None:
    room = unixpath.UNIX_PATH_MAX - 1 - len(str(tmp_path / "")) - 1
    if room < 2:
        pytest.skip(f"{tmp_path} leaves no room under the limit")
    path = tmp_path / ("s" * (room + extra))
    assert unixpath.fits(path) == binds(path)
