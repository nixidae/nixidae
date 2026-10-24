"""
A server for one session: start it, connect, and stop it with the block.

    async with huggorm.worker() as session:
        state = await session.eval(await session.store())

`worker(options, via=...)` takes the server's setup as a
`ServerOptions` and the way to start it as a `via` value. Each `via`
is a small frozen type with its own options:

- `Stdio()`, the default: a new interpreter on a pipe pair. It costs
  an interpreter start and an import of the server, about 0.2 s, and
  needs nothing up front.
- `Multiprocessing()`: a fork of a forkserver that has imported the
  server already, about 26 ms after the first (nixidae/nixidae#551).
  It is the faster choice for a program that starts workers again
  and again, and it is not free: the forkserver starts with the first
  worker, so that worker pays the full start, and a forkserver
  started from a process that has threads already is a hazard
  `multiprocessing` warns about. Call `start_forkserver()` early, before
  the program starts threads.
- `UnixSocket()`: a server on a socket in a private directory. The
  only one a client can reconnect to.
- `huggorm.ssh.Ssh(host)`: the server on another host, over SSH.

A `via` is anything with a `launch(options)` method that gives an async
context manager of a `transport.Dialer`, so a caller can bring its
own. The functions in `huggorm.spawn` and `huggorm.ssh` are the layer
below, for a caller that manages the process or the connection itself.

`attach(server)` connects to a server something else started.
"""

from __future__ import annotations

import contextlib
import multiprocessing.forkserver
import os
import sys
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

from . import spawn, transport
from .server_options import ServerOptions
from .session import AsyncRemoteSession


class Via(Protocol):
    def launch(self, options: ServerOptions) -> AbstractAsyncContextManager[transport.Dialer]:
        """Start a server set up as `options` says, yield a dialer for
        it, and stop it when the block ends."""
        ...


@dataclass(frozen=True, slots=True)
class Stdio:
    """A server in a new interpreter, on its stdin and stdout."""

    python: str = sys.executable

    def launch(self, options: ServerOptions) -> AbstractAsyncContextManager[transport.Dialer]:
        return spawn.spawned_stdio(options, python=self.python)


@dataclass(frozen=True, slots=True)
class Multiprocessing:
    """A server in a `multiprocessing` child, on a socket pair.

    `spawn` starts a new interpreter for each child. It is the method
    that works in a process that was itself forked from one with a
    forkserver."""

    method: Literal["forkserver", "spawn"] = "forkserver"

    def launch(self, options: ServerOptions) -> AbstractAsyncContextManager[transport.Dialer]:
        return spawn.spawned_process(options, method=self.method)


@dataclass(frozen=True, slots=True)
class UnixSocket:
    """A server on a Unix socket in a private directory."""

    def launch(self, options: ServerOptions) -> AbstractAsyncContextManager[transport.Dialer]:
        return _on_socket(options)


@contextlib.asynccontextmanager
async def _on_socket(options: ServerOptions) -> AsyncIterator[transport.Dialer]:
    async with spawn.spawned(options) as path:
        yield transport.unix(path)


def start_forkserver() -> None:
    """Start the forkserver that `Multiprocessing()` forks from, with the
    server imported. Call it early: before the program starts threads,
    and before the first worker, which otherwise pays the start."""
    multiprocessing.forkserver.set_forkserver_preload(spawn.FORKSERVER_PRELOAD)
    multiprocessing.forkserver.ensure_running()


@contextlib.asynccontextmanager
async def worker(
    options: ServerOptions | None = None, *, via: Via | None = None, store_uri: str = "auto"
) -> AsyncIterator[AsyncRemoteSession]:
    """Start a server `via` a transport (`Stdio()` unless given), set up
    as `options` says, and yield a session on it. The server stops with
    the block."""
    async with (
        (via or Stdio()).launch(options or ServerOptions()) as dial,
        AsyncRemoteSession.connect(dial, store_uri=store_uri) as session,
    ):
        yield session


def attach(
    server: str | os.PathLike[str] | transport.Dialer,
    *,
    claim: str | None = None,
    store_uri: str = "auto",
) -> AbstractAsyncContextManager[AsyncRemoteSession]:
    """A session on a server something else started: a Unix socket's
    path, or a dialer from `huggorm.transport`. `claim` adopts the
    leases a previous session detached under that token."""
    return AsyncRemoteSession.connect(server, claim=claim, store_uri=store_uri)
