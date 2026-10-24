"""
A server this process starts, and stops with the block.

The server says when it listens on a pipe (`--ready-fd`), so the caller
waits on that rather than trying the socket until it answers. A server
that dies first closes the pipe, and the caller reads its exit status
instead of a timeout.
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

#: How long a stopped server may take to close its states before it is
#: killed. A call blocked in a system call holds the stop open.
STOP_GRACE = 5.0


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
            # Not `-m huggorm.server`: the package imports that module, so
            # runpy warns on every start, into the caller's stderr.
            process = await anyio.open_process(
                [sys.executable, "-c", "from huggorm.server import main; main()", path, *args,
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
