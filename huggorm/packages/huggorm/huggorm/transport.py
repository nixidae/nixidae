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

import errno
import os
from collections.abc import Awaitable, Callable
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
