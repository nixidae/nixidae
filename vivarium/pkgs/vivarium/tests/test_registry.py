from __future__ import annotations

import os
import socket as sockets
from typing import TYPE_CHECKING

import anyio
import pytest
from test_drive import FakeSession, until_state
from vivarium import liveness, places, registry
from vivarium.cli import drive
from vivarium.control import SOCKET, Op, request

if TYPE_CHECKING:
    from pathlib import Path


def test_the_directory_outlives_a_change_of_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner pins it, then becomes uid 0 in its user namespace."""
    monkeypatch.delenv(places.RUNTIME_ENV, raising=False)
    monkeypatch.delenv(places.STATE_ENV, raising=False)
    monkeypatch.delenv(places.LEGACY_REGISTRY_ENV, raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    before = registry.directory()
    registry.pin()
    monkeypatch.setattr(os, "getuid", lambda: 0)
    assert registry.directory() == before
    assert os.environ[places.LEGACY_REGISTRY_ENV] == str(before)


def test_a_pin_keeps_the_callers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(places.RUNTIME_ENV, str(tmp_path))
    registry.pin()
    assert registry.directory() == tmp_path / places.RUNS


def test_an_entry_lives_as_long_as_its_run(tmp_path: Path) -> None:
    out = tmp_path / "out"
    with registry.registered(out) as entry:
        assert (entry / places.OUT_LINK).readlink() == out.resolve()
        assert entry.parent == registry.directory()
    assert list(registry.directory().iterdir()) == []


def test_a_later_run_into_the_same_out_keeps_its_entry(tmp_path: Path) -> None:
    out = tmp_path / "out"
    with registry.registered(out) as entry:
        link = entry / places.OUT_LINK
        link.unlink()
        link.symlink_to(tmp_path / "elsewhere")
    assert link.readlink() == tmp_path / "elsewhere"


@pytest.mark.anyio
async def test_dead_entries_of_both_layouts_are_removed(tmp_path: Path) -> None:
    runs = places.private(registry.directory())
    (runs / "legacy").symlink_to(tmp_path / "gone")
    places.prepare(tmp_path / "gone-too")
    assert await registry.runs() == []
    assert list(runs.iterdir()) == []


@pytest.mark.anyio
async def test_a_held_run_is_listed_ready(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    session = FakeSession(out=out)
    socket = places.socket(out, SOCKET)
    async with anyio.create_task_group() as group:
        group.start_soon(lambda: drive(session, hold=True))
        await until_state(socket, "ready")
        assert socket.parent == places.run_directory(out)
        [up] = await registry.runs()
        assert (up.out, up.state, up.environment) == (out.resolve(), "ready", None)
        assert (await request(socket, Op.STOP)).ok
    assert await registry.runs() == []


@pytest.mark.anyio
async def test_a_live_run_that_does_not_answer_keeps_its_entry(tmp_path: Path) -> None:
    """Between `places.prepare` and the socket's bind, or with its loop busy."""
    out = tmp_path / "out"
    fd = liveness.hold(out, environ={})
    entry = places.prepare(out)
    [up] = await registry.runs()
    assert (up.out, up.state) == (out.resolve(), registry.UNANSWERED)
    assert entry.is_dir()
    os.close(fd)
    assert await registry.runs() == []
    assert not entry.exists()


@pytest.mark.anyio
async def test_an_old_runner_is_removed_only_when_its_socket_refuses(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    entry = places.prepare(out)
    assert await registry.runs() == []
    assert entry.is_dir()
    with sockets.socket(sockets.AF_UNIX) as bound:
        bound.bind(str(entry / SOCKET))
    assert await registry.runs() == []
    assert not entry.exists()


@pytest.mark.anyio
async def test_a_runner_older_than_places_is_found_in_its_out(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / SOCKET).touch()
    assert places.socket(out, SOCKET) == out / SOCKET
