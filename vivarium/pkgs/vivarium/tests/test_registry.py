from __future__ import annotations

import os
from typing import TYPE_CHECKING

import anyio
import pytest
from test_drive import FakeSession, until_state
from vivarium import registry
from vivarium.cli import drive
from vivarium.control import SOCKET, Op, request

if TYPE_CHECKING:
    from pathlib import Path


def test_the_directory_outlives_a_change_of_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner pins it, then becomes uid 0 in its user namespace."""
    monkeypatch.delenv(registry.ENV, raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    registry.pin()
    monkeypatch.setattr(os, "getuid", lambda: 0)
    assert registry.directory().parent.name == "vivarium-1000"


def test_a_pin_keeps_the_callers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(registry.ENV, str(tmp_path))
    registry.pin()
    assert registry.directory() == tmp_path


def test_a_link_lives_as_long_as_its_run(tmp_path: Path) -> None:
    out = tmp_path / "out"
    with registry.registered(out, tmp_path / "runs"):
        assert registry.entry(out, tmp_path / "runs").readlink() == out
    assert list((tmp_path / "runs").iterdir()) == []


def test_a_later_run_into_the_same_out_keeps_its_link(tmp_path: Path) -> None:
    out = tmp_path / "out"
    runs = tmp_path / "runs"
    with registry.registered(out, runs):
        link = registry.entry(out, runs)
        link.unlink()
        link.symlink_to(tmp_path / "elsewhere")
    assert link.readlink() == tmp_path / "elsewhere"


@pytest.mark.anyio
async def test_a_dead_runs_link_is_removed(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    registry.entry(tmp_path / "gone", runs).symlink_to(tmp_path / "gone")
    assert await registry.runs(runs) == []
    assert list(runs.iterdir()) == []


@pytest.mark.anyio
async def test_a_held_run_is_listed_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = tmp_path / "runs"
    monkeypatch.setattr(registry, "directory", lambda: runs)
    out = tmp_path / "out"
    out.mkdir()
    session = FakeSession(out=out)
    async with anyio.create_task_group() as group:
        group.start_soon(lambda: drive(session, hold=True))
        await until_state(out / SOCKET, "ready")
        [up] = await registry.runs(runs)
        assert (up.out, up.state) == (out.resolve(), "ready")
        assert (await request(out / SOCKET, Op.STOP)).ok
    assert await registry.runs(runs) == []
