"""
The byte streams a server and a client talk over, and how a client
gets one.

The protocol (`huggorm.protocol.Channel`) reads and writes any
`anyio.abc.ByteStream`. A Unix socket is one source. A pipe pair is
another: the stdin and stdout of a server this process started, or of
one an SSH session runs. A pipe carries one connection, so a client
on one cannot reconnect, and `once` says so instead of dialling again.
"""

from __future__ import annotations

import contextlib
import errno
import os
import shlex
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeAlias

import anyio
import anyio.abc

from .protocol import ConnectionLost

#: How a client opens a connection. `NixClient.bind` calls it once per
#: connection, so a socket redials and a pipe refuses.
Dialer: TypeAlias = Callable[[], Awaitable[anyio.abc.ByteStream]]

_READ_SIZE = 65536


def unix(path: str | os.PathLike[str]) -> Dialer:
    """Dial the Unix socket at `path`, once per call."""
    address = os.fspath(path)

    async def dial() -> anyio.abc.ByteStream:
        return await anyio.connect_unix(address)

    return dial


def once(stream: anyio.abc.ByteStream, label: str) -> Dialer:
    """Hand out `stream` once. A second dial raises ConnectionLost:
    the pipe under `label` carries one connection, and nothing can
    open it again."""
    given = False

    async def dial() -> anyio.abc.ByteStream:
        nonlocal given
        if given:
            raise ConnectionLost(f"{label} carries one connection, and it is used")
        given = True
        return stream

    return dial


class FdStream(anyio.abc.ByteStream):
    """A byte stream over a read descriptor and a write descriptor.

    For the server end of a pipe pair: its own stdin and stdout. Both
    descriptors become non-blocking, and a read or a write that would
    block waits on the event loop (`anyio.wait_readable`), not on a
    thread. The stream owns both descriptors and closes them."""

    def __init__(self, read_fd: int, write_fd: int) -> None:
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)
        self._read_fd: int | None = read_fd
        self._write_fd: int | None = write_fd

    async def receive(self, max_bytes: int = _READ_SIZE) -> bytes:
        while True:
            if self._read_fd is None:
                raise anyio.ClosedResourceError
            try:
                data = os.read(self._read_fd, max_bytes)
            except BlockingIOError:
                await anyio.wait_readable(self._read_fd)
                continue
            if not data:
                raise anyio.EndOfStream
            return data

    async def send(self, item: bytes) -> None:
        view = memoryview(item)
        while view:
            if self._write_fd is None:
                raise anyio.ClosedResourceError
            try:
                written = os.write(self._write_fd, view)
            except BlockingIOError:
                await anyio.wait_writable(self._write_fd)
                continue
            except OSError as e:
                if e.errno == errno.EPIPE:
                    raise anyio.BrokenResourceError from e
                raise
            view = view[written:]

    async def send_eof(self) -> None:
        if self._write_fd is not None:
            os.close(self._write_fd)
            self._write_fd = None

    async def aclose(self) -> None:
        await self.send_eof()
        if self._read_fd is not None:
            os.close(self._read_fd)
            self._read_fd = None


#: How long a closed `ssh -W` gets to exit before it is terminated.
SSH_STOP_GRACE = 5.0


class SshFailed(ConnectionError):
    """`ssh -W` ended before the server answered: the host, the login or
    the socket refused. `stderr` is what ssh said."""

    def __init__(self, command: Sequence[str], returncode: int | None, stderr: str) -> None:
        super().__init__(f"{shlex.join(command)} exited {returncode}: {stderr.strip() or 'no message'}")
        self.returncode = returncode
        self.stderr = stderr


class _SshProcess(anyio.abc.ByteStream):
    """The stdin and stdout of one `ssh -W` process.

    ssh starts before it knows whether the connection works, so a refused
    socket, a dead host and a failed login all arrive as an end of
    stream. Before the first byte that end raises `SshFailed`; after it,
    a plain end of stream, which the client reads as a lost connection."""

    def __init__(self, process: anyio.abc.Process, command: Sequence[str]) -> None:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise RuntimeError("open_process gave no stdin, stdout or stderr pipe")
        self._process = process
        self._command = command
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._answered = False

    async def receive(self, max_bytes: int = _READ_SIZE) -> bytes:
        try:
            data = await self._stdout.receive(max_bytes)
        except anyio.EndOfStream:
            if self._answered:
                raise
            raise await self._failure() from None
        self._answered = True
        return data

    async def _failure(self) -> SshFailed:
        # stderr first: a process blocked on a full stderr pipe never exits.
        stderr = b""
        if self._process.stderr is not None:
            with (
                anyio.move_on_after(SSH_STOP_GRACE),
                contextlib.suppress(anyio.EndOfStream, anyio.ClosedResourceError),
            ):
                while chunk := await self._process.stderr.receive():
                    stderr += chunk
        with anyio.move_on_after(SSH_STOP_GRACE):
            await self._process.wait()
        return SshFailed(self._command, self._process.returncode, stderr.decode(errors="replace"))

    async def send(self, item: bytes) -> None:
        await self._stdin.send(item)

    async def send_eof(self) -> None:
        await self._stdin.aclose()

    async def aclose(self) -> None:
        with anyio.CancelScope(shield=True):
            with contextlib.suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
                await self._stdin.aclose()
            with anyio.move_on_after(SSH_STOP_GRACE):
                await self._process.wait()
            if self._process.returncode is None:
                self._process.terminate()
                with anyio.move_on_after(SSH_STOP_GRACE):
                    await self._process.wait()
            if self._process.returncode is None:
                self._process.kill()
            await self._process.aclose()


def openssh(host: str, path: str, *, ssh: str = "ssh", options: Sequence[str] = ()) -> Dialer:
    """Dial the Unix socket at `path` on `host` through the OpenSSH
    client, `ssh -W path host`, one process per dial.

    It reads the user's `~/.ssh/config`: keys, agent, `ProxyJump`, and a
    `ControlMaster`, through which a dial costs about two round trips
    instead of a handshake (0.66 s against 3.5 s at 300 ms RTT, measured
    between two QEMU guests). `options` go before `-W` as they are, such
    as `-o ControlPath=...`. `BatchMode=yes` is always set: a dial never
    waits on a prompt."""
    command = [ssh, "-o", "BatchMode=yes", *options, "-W", path, host]

    async def dial() -> anyio.abc.ByteStream:
        process = await anyio.open_process(command)
        return _SshProcess(process, command)

    return dial
