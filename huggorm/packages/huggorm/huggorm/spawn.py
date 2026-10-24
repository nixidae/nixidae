"""
A server this process starts, and stops with the block.

`spawned` runs a server on a Unix socket. The server says when it
listens on a pipe (`--ready-fd`), so the caller waits on that rather
than trying the socket until it answers. A server that dies first
closes the pipe, and the caller reads its exit status instead of a
timeout.

`spawned_stdio` runs a server on its own stdin and stdout, for one
connection. WELCOME is the ready signal, and the server exits when the
caller closes its stdin.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator

import anyio
from anyio.streams.stapled import StapledByteStream

from . import transport

#: How long a stopped server may take to close its states before it is
#: killed. A call blocked in a system call holds the stop open.
STOP_GRACE = 5.0


#: The server's entry point. Not `-m huggorm.server`: the package
#: imports that module, so runpy warns on every start, into the
#: caller's stderr.
_MAIN = "from huggorm.server import main; main()"


class ServerExited(RuntimeError):
    """The server exited before it listened. Its own stderr says why."""


@contextlib.asynccontextmanager
async def spawned(*args: str) -> AsyncIterator[str]:
    """Run `huggorm.server` with `args` on a socket in a
    private directory, and yield the socket's path once it listens.

    The server inherits stderr, so its reason for failing reaches the
    caller's terminal. Leaving the block stops it with SIGTERM."""
    directory = await anyio.to_thread.run_sync(
        lambda: tempfile.mkdtemp(prefix="huggorm-"))
    path = os.path.join(directory, "server.sock")
    read_fd, write_fd = os.pipe()
    try:
        try:
            process = await anyio.open_process(
                [sys.executable, "-c", _MAIN, path, *args,
                 "--ready-fd", str(write_fd)],
                stdin=subprocess.DEVNULL, stdout=None, stderr=None,
                pass_fds=(write_fd,))
        finally:
            os.close(write_fd)
        async with process:
            try:
                await anyio.wait_readable(read_fd)
                if not os.read(read_fd, 64):
                    raise ServerExited(
                        f"the server exited with status {await process.wait()} "
                        f"before it listened")
                yield path
            finally:
                with anyio.CancelScope(shield=True):
                    if process.returncode is None:
                        process.terminate()
                        with anyio.move_on_after(STOP_GRACE):
                            await process.wait()
                        if process.returncode is None:
                            process.kill()
                            await process.wait()
    finally:
        os.close(read_fd)
        await anyio.to_thread.run_sync(lambda: shutil.rmtree(directory))


@contextlib.asynccontextmanager
async def spawned_stdio(*args: str) -> AsyncIterator[transport.Dialer]:
    """Run `huggorm.server --stdio` with `args`, and yield a dialer for
    its one connection.

    The server inherits stderr. Leaving the block closes its stdin; the
    server ends its connection at EOF and exits. One that has not exited
    after `STOP_GRACE` seconds is terminated, then killed."""
    process = await anyio.open_process(
        [sys.executable, "-c", _MAIN, "--stdio", *args], stderr=None)
    async with process:
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("open_process gave no stdin or stdout pipe")
        try:
            yield transport.once(StapledByteStream(process.stdin, process.stdout),
                                 "a stdio server")
        finally:
            with anyio.CancelScope(shield=True):
                await process.stdin.aclose()
                with anyio.move_on_after(STOP_GRACE):
                    await process.wait()
                if process.returncode is None:
                    process.terminate()
                    with anyio.move_on_after(STOP_GRACE):
                        await process.wait()
                if process.returncode is None:
                    process.kill()
                    await process.wait()
