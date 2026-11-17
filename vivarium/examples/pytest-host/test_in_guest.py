"""Tests that run inside a guest, `guest=` (nixidae/nixidae#1413).

The whole test runs there, fixtures included: as root, on the guest's
kernel, with this project at its own path and this Python. The host's
pytest reports each as it reports any test. Every test of one scope runs
in one pytest in the guest, so a module fixture is made once.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import anyio
import pytest

pytestmark = pytest.mark.vivarium("one", guest="vm", scope="module")


@pytest.fixture(scope="module")
def flag() -> Path:
    return Path("/root/vivarium-flag")


def test_runs_in_the_guest_as_root() -> None:
    assert socket.gethostname() == "vm"
    assert os.getuid() == 0


def test_sees_the_project_at_its_own_path() -> None:
    assert Path(__file__).exists()
    assert Path(__file__).with_name("machines.nix").exists()


@pytest.mark.anyio
async def test_async_runs_too() -> None:
    await anyio.sleep(0)
    assert await anyio.Path("/proc/1/comm").read_text() == "systemd\n"


class TestOneGuest:
    def test_writes(self, flag: Path) -> None:
        flag.write_text("written")

    def test_the_next_test_sees_the_write(self, flag: Path) -> None:
        assert flag.read_text() == "written"
