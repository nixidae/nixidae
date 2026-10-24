"""
A server over SSH: `huggorm.server --stdio` on another host, and the
SSH channel as its one connection (nixidae/nixidae#551).

`Ssh(host)` is the `via` for `huggorm.worker`, and `SshConnection` the
one over a connection the caller opened and manages itself.
`connected(host)` and `spawned(connection, command)` are the layer
below them.

asyncssh opens a connection the way the OpenSSH client does by
default: `~/.ssh/config`, the agent, the default keys and
`~/.ssh/known_hosts`.

HELLO compares build identities, so the remote huggorm must be the
same build: the same store path on a host of the same platform.

asyncssh runs on asyncio only, so a client on this transport is too.
Needs the `ssh` extra.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

import anyio
import anyio.abc
import asyncssh

from . import transport
from .server_options import ServerOptions
from .spawn import STOP_GRACE

_READ_SIZE = 65536


class SshStream(anyio.abc.ByteStream):
    """An asyncssh reader and writer as one byte stream: the stdout and
    stdin of a remote process, or the stdin and stdout of a session a
    server answers. Both are opened with `encoding=None`."""

    def __init__(
        self, reader: asyncssh.SSHReader[bytes], writer: asyncssh.SSHWriter[bytes]
    ) -> None:
        self._reader = reader
        self._writer = writer

    async def receive(self, max_bytes: int = _READ_SIZE) -> bytes:
        try:
            data = await self._reader.read(max_bytes)
        except (asyncssh.Error, BrokenPipeError, ConnectionError) as e:
            raise anyio.BrokenResourceError from e
        if not data:
            raise anyio.EndOfStream
        return data

    async def send(self, item: bytes) -> None:
        try:
            self._writer.write(item)
            await self._writer.drain()
        except (asyncssh.Error, BrokenPipeError, ConnectionError) as e:
            raise anyio.BrokenResourceError from e

    async def send_eof(self) -> None:
        if self._writer.can_write_eof():
            self._writer.write_eof()

    async def aclose(self) -> None:
        with contextlib.suppress(asyncssh.Error, BrokenPipeError, ConnectionError):
            await self.send_eof()


def server_command(options: ServerOptions | None = None, *,
                   python: str = sys.executable) -> str:
    """The shell command that runs `huggorm.server --stdio` set up as
    `options` says, under `python`, this process's interpreter unless
    given."""
    main = "from huggorm.server import main; main()"
    argv = (options or ServerOptions()).argv()
    return shlex.join([python, "-c", main, "--stdio", *argv])


@contextlib.asynccontextmanager
async def spawned(
    connection: asyncssh.SSHClientConnection, command: str
) -> AsyncIterator[transport.Dialer]:
    """Run `command` (see `server_command`) over `connection`, and yield
    a dialer for the server's one connection.

    Leaving the block sends EOF; the server ends its connection and
    exits. One that has not exited after `STOP_GRACE` seconds is
    terminated, then killed. The remote server's stderr arrives here."""
    # A duplicate of fd 2: asyncssh closes a redirect target at EOF.
    process: Any = await connection.create_process(command, encoding=None, stderr=os.dup(2))
    try:
        stream = SshStream(process.stdout, process.stdin)
        yield transport.once(stream, f"the ssh server {command!r}")
    finally:
        with anyio.CancelScope(shield=True):
            process.stdin.write_eof()
            with anyio.move_on_after(STOP_GRACE):
                await process.wait_closed()
            if process.returncode is None:
                process.terminate()
                with anyio.move_on_after(STOP_GRACE):
                    await process.wait_closed()
            if process.returncode is None:
                process.kill()
            process.close()


@contextlib.asynccontextmanager
async def connected(
    host: str,
    *,
    username: str | None = None,
    port: int | None = None,
    password: str | None = None,
    command: str | None = None,
    **options: Any,
) -> AsyncIterator[transport.Dialer]:
    """Connect to `host` with asyncssh's defaults, run the server there,
    and yield a dialer for its one connection.

    `command` defaults to `server_command()`: this interpreter's path,
    which the remote host has when it holds the same store paths.
    `options` go to `asyncssh.connect` as they are, for anything the
    defaults do not cover; a `None` argument leaves asyncssh's default.
    The connection closes with the block."""
    given = {"username": username, "port": port, "password": password}
    # Any: asyncssh's option names, passed through unread.
    arguments: dict[str, Any] = {k: v for k, v in given.items() if v is not None}
    async with (
        asyncssh.connect(host, **arguments, **options) as connection,
        spawned(connection, command or server_command()) as dial,
    ):
        yield dial


@dataclass(frozen=True, slots=True)
class Ssh:
    """A server on `host`, over a connection this opens with asyncssh's
    defaults and closes with the block.

    `python` is the remote interpreter: this process's, which the host
    has when it holds the same store paths. `connect_options` go to
    `asyncssh.connect` as they are."""

    host: str
    username: str | None = None
    port: int | None = None
    password: str | None = None
    python: str = sys.executable
    # Any: asyncssh's option names, passed through unread.
    connect_options: Mapping[str, Any] = field(default_factory=dict)

    def launch(self, options: ServerOptions
               ) -> AbstractAsyncContextManager[transport.Dialer]:
        return connected(self.host, username=self.username, port=self.port,
                         password=self.password,
                         command=server_command(options, python=self.python),
                         **self.connect_options)


@dataclass(frozen=True, slots=True)
class SshConnection:
    """A server over `connection`, which the caller opened and closes."""

    connection: asyncssh.SSHClientConnection
    python: str = sys.executable

    def launch(self, options: ServerOptions
               ) -> AbstractAsyncContextManager[transport.Dialer]:
        return spawned(self.connection, server_command(options, python=self.python))
