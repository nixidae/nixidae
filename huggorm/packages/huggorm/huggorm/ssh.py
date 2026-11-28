"""
A server over SSH: `huggorm.server --stdio` on another host, and the
SSH channel as its one connection (nixidae/nixidae#551).

`Ssh(host)` is the `via` for `huggorm.worker`, and `SshConnection` the
one over a connection the caller opened and manages itself.
`connected(host)` and `spawned(connection, command)` are the layer
below them. Each starts a server per connection: a cold one.

`forwarded(connection, path)` and `socket_dialer(host, path)` reach a
server that already runs, `huggorm.server PATH` on the far host, over
SSH Unix socket forwarding: a warm one. Several clients share it, each
with its own state, and a client that reconnects claims its state
again.

`sessions(connection)` reaches a server that listens on SSH itself,
`huggorm.server --ssh-listen`: each dial is a new session on the link.

asyncssh opens a connection the way the OpenSSH client does by
default: `~/.ssh/config`, the agent, the default keys and
`~/.ssh/known_hosts`.

HELLO compares build identities, so the remote huggorm must be the
same build: the same store path on a host of the same platform.
`provisioned(connection)` starts a cold server and, on a host that
lacks the interpreter, copies its closure there first (`huggorm.ship`).
`connected`, `Ssh` and `SshConnection` start servers that way.

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
from typing import Any, Final

import anyio
import anyio.abc
import asyncssh

from . import ship, transport
from .server_options import ServerOptions
from .spawn import STOP_GRACE

_READ_SIZE = 65536
PRESENT: Final = b"R"
ABSENT: Final = b"M"


class SshStream(anyio.abc.ByteStream):
    """An asyncssh reader and writer as one byte stream: the stdout and
    stdin of a remote process, or the stdin and stdout of a session a
    server answers. Both are opened with `encoding=None`."""

    def __init__(self, reader: asyncssh.SSHReader[bytes], writer: asyncssh.SSHWriter[bytes]) -> None:
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


def server_command(options: ServerOptions | None = None, *, python: str = sys.executable) -> str:
    """The shell command that runs `huggorm.server --stdio` set up as
    `options` says, under `python`, this process's interpreter unless
    given."""
    main = "from huggorm.server import main; main()"
    argv = (options or ServerOptions()).argv()
    return shlex.join([python, "-c", main, "--stdio", *argv])


def guarded_command(options: ServerOptions | None = None, *, python: str = sys.executable) -> str:
    """`server_command` behind a check that `python` is on the far host.

    The command writes one byte before anything else: `PRESENT`, then
    the server runs; or `ABSENT`, then it exits. `sh -c`, so the
    user's login shell only has to pass one quoted string on."""
    inner = (
        f"test -x {shlex.quote(python)} || {{ printf {ABSENT.decode()}; exit; }}; "
        f"printf {PRESENT.decode()}; exec {server_command(options, python=python)}"
    )
    return shlex.join(["sh", "-c", inner])


async def _start(connection: asyncssh.SSHClientConnection, command: str) -> Any:
    # Any: asyncssh's process type, with stdout and stdin read below.
    # A duplicate of fd 2: asyncssh closes a redirect target at EOF.
    return await connection.create_process(command, encoding=None, stderr=os.dup(2))


async def _stop(process: Any) -> None:
    with anyio.CancelScope(shield=True):
        # A guard that answered ABSENT has closed its channel already.
        with contextlib.suppress(asyncssh.Error, BrokenPipeError, ConnectionError):
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


async def _answer(process: Any, command: str) -> bytes:
    answer: bytes = await process.stdout.read(1)
    if answer not in (PRESENT, ABSENT):
        await process.wait_closed()
        raise transport.SshFailed([command], process.returncode, f"the guard answered {answer!r}, not a byte")
    return answer


@contextlib.asynccontextmanager
async def spawned(connection: asyncssh.SSHClientConnection, command: str) -> AsyncIterator[transport.Dialer]:
    """Run `command` (see `server_command`) over `connection`, and yield
    a dialer for the server's one connection.

    Leaving the block sends EOF; the server ends its connection and
    exits. One that has not exited after `STOP_GRACE` seconds is
    terminated, then killed. The remote server's stderr arrives here."""
    process = await _start(connection, command)
    try:
        yield transport.once(SshStream(process.stdout, process.stdin), f"the ssh server {command!r}")
    finally:
        await _stop(process)


@contextlib.asynccontextmanager
async def provisioned(
    connection: asyncssh.SSHClientConnection,
    options: ServerOptions | None = None,
    *,
    python: str = sys.executable,
    daemon_socket: str = ship.DAEMON_SOCKET,
    source: str | None = None,
) -> AsyncIterator[transport.Dialer]:
    """`spawned` for `guarded_command(options, python=python)`, and on a
    far host that lacks `python`, a copy of its closure first
    (`ship.shipped`, from the store `source` names to the daemon at
    `daemon_socket`), then a second start.

    A host that holds the interpreter costs nothing more than the one
    byte the guard writes. When `python` is this interpreter, the copy
    also checks that huggorm comes from its closure."""
    command = guarded_command(options, python=python)
    files = ship.own_files() if python == sys.executable else ()
    async with contextlib.AsyncExitStack() as stack:
        process = await _start(connection, command)
        stack.push_async_callback(_stop, process)
        if await _answer(process, command) == ABSENT:
            await stack.enter_async_context(
                ship.shipped(connection, python, files=files, socket=daemon_socket, source=source)
            )
            process = await _start(connection, command)
            stack.push_async_callback(_stop, process)
            if await _answer(process, command) == ABSENT:
                raise ship.NotShippable(f"{python} is still absent on the far host after the copy")
        yield transport.once(SshStream(process.stdout, process.stdin), f"the ssh server {command!r}")


async def _open_socket(connection: asyncssh.SSHClientConnection, path: str) -> SshStream:
    try:
        reader, writer = await connection.open_unix_connection(path)
    except asyncssh.ChannelOpenError as e:
        # What `transport.unix` raises for a socket nobody serves.
        raise ConnectionRefusedError(f"ssh refused a connection to {path!r}: {e.reason}") from e
    return SshStream(reader, writer)


def forwarded(connection: asyncssh.SSHClientConnection, path: str) -> transport.Dialer:
    """Dial the Unix socket at `path` on the far end of `connection`,
    once per call: a long-lived `huggorm.server PATH` there.

    The caller owns `connection`. Each dial is its own channel, so a
    client redials while the connection lives. The SSH server must
    allow Unix socket forwarding (`AllowStreamLocalForwarding`, on by
    default in OpenSSH)."""

    async def dial() -> anyio.abc.ByteStream:
        return await _open_socket(connection, path)

    return dial


class _SessionStream(SshStream):
    """A session's stdout and stdin; closing it ends the session."""

    def __init__(self, process: asyncssh.SSHClientProcess[bytes]) -> None:
        super().__init__(process.stdout, process.stdin)
        self._process = process

    async def aclose(self) -> None:
        # No wait for the server to close the channel: that costs a round
        # trip, and nothing the server sends after EOF is read.
        await super().aclose()
        self._process.close()


def sessions(connection: asyncssh.SSHClientConnection) -> transport.Dialer:
    """Dial `huggorm.server --ssh-listen` (`huggorm.ssh_server`) over
    `connection`, with a new SSH session per call.

    The caller owns `connection` and keeps it: a client redials over the
    same link with no new SSH handshake, which costs several round trips."""

    async def dial() -> anyio.abc.ByteStream:
        process: asyncssh.SSHClientProcess[bytes] = await connection.create_process(encoding=None)
        return _SessionStream(process)

    return dial


class _OwningStream(SshStream):
    """A channel that closes its own SSH connection when it closes."""

    def __init__(self, stream: SshStream, connection: asyncssh.SSHClientConnection) -> None:
        super().__init__(stream._reader, stream._writer)
        self._connection = connection

    async def aclose(self) -> None:
        with anyio.CancelScope(shield=True):
            await super().aclose()
            self._connection.close()
            with anyio.move_on_after(STOP_GRACE):
                await self._connection.wait_closed()


def socket_dialer(
    host: str,
    path: str,
    *,
    username: str | None = None,
    port: int | None = None,
    password: str | None = None,
    **options: Any,
) -> transport.Dialer:
    """Dial the Unix socket at `path` on `host`, with a new SSH
    connection per call. The stream owns that connection and closes it.

    For a client that must outlive its SSH link: a dropped link ends the
    huggorm connection, and the next dial opens a new link. A detached
    session claims its state again over it. `options` go to
    `asyncssh.connect` as they are; `keepalive_interval` makes a link
    that stops answering fail instead of hanging."""
    given = {"username": username, "port": port, "password": password}
    # Any: asyncssh's option names, passed through unread.
    arguments: dict[str, Any] = {k: v for k, v in given.items() if v is not None}

    async def dial() -> anyio.abc.ByteStream:
        connection = await asyncssh.connect(host, **arguments, **options)
        try:
            stream = await _open_socket(connection, path)
        except BaseException:
            connection.close()
            raise
        return _OwningStream(stream, connection)

    return dial


@contextlib.asynccontextmanager
async def connected(
    host: str,
    *,
    username: str | None = None,
    port: int | None = None,
    password: str | None = None,
    server: ServerOptions | None = None,
    python: str = sys.executable,
    command: str | None = None,
    **options: Any,
) -> AsyncIterator[transport.Dialer]:
    """Connect to `host` with asyncssh's defaults, run the server there,
    and yield a dialer for its one connection.

    The server is `provisioned(connection, server, python=python)`, so a
    host without `python` gets its closure first; `command` runs instead
    when given, and is never guarded. `options` go to `asyncssh.connect`
    as they are, for anything the defaults do not cover; a `None`
    argument leaves asyncssh's default. The connection closes with the
    block."""
    given = {"username": username, "port": port, "password": password}
    # Any: asyncssh's option names, passed through unread.
    arguments: dict[str, Any] = {k: v for k, v in given.items() if v is not None}
    async with asyncssh.connect(host, **arguments, **options) as connection:
        started = (
            spawned(connection, command) if command is not None else provisioned(connection, server, python=python)
        )
        async with started as dial:
            yield dial


@dataclass(frozen=True, slots=True)
class Ssh:
    """A server on `host`, over a connection this opens with asyncssh's
    defaults and closes with the block.

    `python` is the remote interpreter: this process's unless given,
    copied to a host that lacks it (`provisioned`). `connect_options`
    go to `asyncssh.connect` as they are."""

    host: str
    username: str | None = None
    port: int | None = None
    password: str | None = None
    python: str = sys.executable
    # Any: asyncssh's option names, passed through unread.
    connect_options: Mapping[str, Any] = field(default_factory=dict)

    def launch(self, options: ServerOptions) -> AbstractAsyncContextManager[transport.Dialer]:
        return connected(
            self.host,
            username=self.username,
            port=self.port,
            password=self.password,
            server=options,
            python=self.python,
            **self.connect_options,
        )


@dataclass(frozen=True, slots=True)
class SshConnection:
    """A server over `connection`, which the caller opened and closes,
    started as `provisioned` starts one."""

    connection: asyncssh.SSHClientConnection
    python: str = sys.executable

    def launch(self, options: ServerOptions) -> AbstractAsyncContextManager[transport.Dialer]:
        return provisioned(self.connection, options, python=self.python)
