"""A temporary overlay leaves nothing behind, whatever ends it
(nixidae/nixidae#1066)."""

import fcntl
import logging
import os
import pathlib
import signal
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager

import anyio
import anyio.abc
import pytest
from test_namespace import _namespaces

from huggorm import TEMPORARY, ServerOptions, namespace, transport, worker
from huggorm.namespace import OWNER_FILE, TEMPORARY_PREFIX, remove_layers, sweep

HOLDER = """
import sys, time
from huggorm.namespace import hold
hold(sys.argv[1])
print("held", flush=True)
time.sleep(600)
"""

#: A worker on a temporary overlay that says when its store is open,
#: then makes one more call for each line on stdin.
CHILD = """
import sys, anyio
from huggorm import TEMPORARY, ServerOptions, worker

async def main():
    async with worker(ServerOptions(overlay=TEMPORARY, load_config=True)) as session:
        store = await session.store()
        path = await store.parse_store_path(sys.argv[1])
        assert await store.is_valid_path(path)
        print("ready", flush=True)
        while await anyio.to_thread.run_sync(sys.stdin.readline):
            await store.is_valid_path(path)

anyio.run(main)
"""


@pytest.fixture
def tmpdir_here(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """The system's temporary directory, for this process and its
    children, is `tmp_path`."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return tmp_path


def _roots(parent: pathlib.Path) -> list[pathlib.Path]:
    return sorted(parent.glob(f"{TEMPORARY_PREFIX}*"))


async def _holder(root: str) -> anyio.abc.Process:
    process = await anyio.open_process([sys.executable, "-c", HOLDER, root])
    assert process.stdout is not None
    assert await process.stdout.receive() == b"held\n"
    return process


async def test_the_sweep_leaves_a_held_root_and_removes_it_when_the_holder_dies(tmpdir_here: pathlib.Path) -> None:
    root = tempfile.mkdtemp(prefix=TEMPORARY_PREFIX)
    async with await _holder(root) as process:
        assert sweep(min_age=0) == []
        assert os.path.isdir(root)
        process.kill()
        await process.wait()
    assert sweep(min_age=0) == [root]
    assert not os.path.lexists(root)


async def test_the_sweep_leaves_a_young_root(tmpdir_here: pathlib.Path) -> None:
    root = tempfile.mkdtemp(prefix=TEMPORARY_PREFIX)
    assert sweep() == []
    assert sweep(min_age=0) == [root]


def test_the_sweep_removes_a_root_without_a_lock_and_leaves_other_names(tmpdir_here: pathlib.Path) -> None:
    root = tempfile.mkdtemp(prefix=TEMPORARY_PREFIX)
    other = tempfile.mkdtemp(prefix="pynix-overlay-")
    assert sweep(min_age=0) == [root]
    assert os.path.isdir(other)


def test_a_hold_after_a_sweep_removed_the_root_fails(tmpdir_here: pathlib.Path) -> None:
    root, fd = namespace.make_temporary_root()
    os.close(fd)
    assert sweep(min_age=0) == [root]
    with pytest.raises(FileNotFoundError):
        namespace.hold(root)


class _Fails:
    """A via whose server fails to start."""

    def launch(self, options: ServerOptions) -> AbstractAsyncContextManager[transport.Dialer]:
        assert isinstance(options.overlay, str)
        assert options.remove_overlay
        assert os.path.isfile(os.path.join(options.overlay, OWNER_FILE))
        raise RuntimeError("the server failed")


async def test_a_worker_removes_its_temporary_root_when_the_work_fails(tmpdir_here: pathlib.Path) -> None:
    with pytest.raises(RuntimeError, match="the server failed"):
        async with worker(ServerOptions(overlay=TEMPORARY), via=_Fails()):
            pass
    assert _roots(tmpdir_here) == []


async def test_a_cleanup_failure_is_logged_and_never_hides_the_error(
    tmpdir_here: pathlib.Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(root: str | os.PathLike[str]) -> None:
        raise PermissionError(13, "refused", os.fspath(root))

    monkeypatch.setattr(namespace, "remove_layers", refuse)
    with (
        caplog.at_level(logging.WARNING, logger="huggorm.namespace"),
        pytest.raises(RuntimeError, match="the server failed"),
    ):
        async with worker(ServerOptions(overlay=TEMPORARY), via=_Fails()):
            pass
    assert "cannot remove the overlay layers" in caplog.text
    [root] = _roots(tmpdir_here)
    monkeypatch.undo()
    assert sweep(root.parent, min_age=0) == [str(root)]


def test_a_temporary_overlay_has_no_command_line() -> None:
    with pytest.raises(ValueError, match="TEMPORARY"):
        ServerOptions(overlay=TEMPORARY).argv()


# The live tests below kill a worker on a real overlay.


@pytest.fixture
async def child(tmpdir_here: pathlib.Path) -> AsyncIterator[anyio.abc.Process]:
    """A process with a worker on a temporary overlay, its store open."""
    probe = tmpdir_here / "probe"
    if reason := await _namespaces(probe):
        pytest.skip(reason)
    await anyio.to_thread.run_sync(remove_layers, probe)
    held = str(pathlib.PurePath(*pathlib.Path(sys.executable).resolve().parts[:4]))
    async with await anyio.open_process([sys.executable, "-c", CHILD, held], stderr=None) as process:
        assert process.stdout is not None
        with anyio.fail_after(60):
            assert await process.stdout.receive() == b"ready\n"
        try:
            yield process
        finally:
            if process.returncode is None:
                process.kill()


async def _released(root: pathlib.Path) -> None:
    """Wait until no process holds `root`, then return. A remover holds
    the lock until it has removed the root."""
    fd = os.open(root / OWNER_FILE, os.O_RDONLY | os.O_CLOEXEC)
    try:
        with anyio.fail_after(30):
            await anyio.to_thread.run_sync(fcntl.flock, fd, fcntl.LOCK_EX, abandon_on_cancel=True)
    finally:
        os.close(fd)


def _server_of(process: anyio.abc.Process) -> int:
    children = pathlib.Path(f"/proc/{process.pid}/task/{process.pid}/children")
    [pid] = children.read_text().split()
    return int(pid)


@pytest.mark.live
async def test_a_killed_parent_leaves_nothing(tmpdir_here: pathlib.Path, child: anyio.abc.Process) -> None:
    """The server sees its connection end, exits, and removes the root."""
    [root] = _roots(tmpdir_here)
    assert await anyio.Path(root / "upper").is_dir()
    child.kill()
    await _released(root)
    assert _roots(tmpdir_here) == []


@pytest.mark.live
async def test_a_killed_server_leaves_nothing(tmpdir_here: pathlib.Path, child: anyio.abc.Process) -> None:
    """The worker's next call fails, and the worker removes the root.
    An idle session learns of a dead server only when it calls."""
    assert len(_roots(tmpdir_here)) == 1
    os.kill(_server_of(child), signal.SIGKILL)
    assert child.stdin is not None
    await child.stdin.send(b"call\n")
    with anyio.fail_after(30):
        await child.wait()
    assert _roots(tmpdir_here) == []


@pytest.mark.live
async def test_the_sweep_removes_what_a_killed_parent_and_server_leave(
    tmpdir_here: pathlib.Path, child: anyio.abc.Process
) -> None:
    [root] = _roots(tmpdir_here)
    server = _server_of(child)
    os.kill(server, signal.SIGSTOP)
    child.kill()
    await child.wait()
    os.kill(server, signal.SIGKILL)
    await _released(root)
    assert await anyio.Path(root / "upper").is_dir()
    assert sweep(min_age=0) == [str(root)]
    assert _roots(tmpdir_here) == []
