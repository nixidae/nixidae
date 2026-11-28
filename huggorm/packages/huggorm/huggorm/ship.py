"""
The server's closure, copied to a host that lacks it, over an SSH link
the client already holds.

asyncssh forwards a Unix socket on this host to the far host's Nix
daemon socket, and `copy_closure` pushes to a `unix://` store on it. A
forwarded `unix://` store takes a push and fails a read, because it
reads NAR contents from this host's /nix/store (`nix-tcp-store`
measures it). A push reads nothing from the far side.

The far daemon decides trust. The copy asks it to skip signature
checks, and Nix's daemon ignores that from a user outside
`trusted-users` (`src/libstore/daemon.cc`, `AddMultipleToStore`): that
user gets Nix's own refusal of each unsigned path.

The far daemon holds a temporary root on each path it received while
the connection lives, so `shipped` keeps it open for its whole block:
a garbage collection between the copy and the server's start would
otherwise take the paths back.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator, Sequence
from typing import Final

import anyio
import asyncssh

import huggorm_bindings
import huggorm_generated
from huggorm_bindings import StorePath
from huggorm_generated import AsyncStore

from .session import AsyncSession

DAEMON_SOCKET: Final = "/nix/var/nix/daemon-socket/socket"


class NotShippable(RuntimeError):
    """The server cannot run from what a copy of the closure carries."""


def own_files() -> tuple[str, ...]:
    """A file of each package the server imports: they must come from
    the interpreter's closure, or a copy of it starts no server."""
    return (__file__, huggorm_bindings.__file__ or "", huggorm_generated.__file__ or "")


def store_path_of(path: str, store_dir: str) -> str | None:
    """The store path `path` lies in, or None for a path outside
    `store_dir`."""
    prefix = store_dir.rstrip("/") + "/"
    if not path.startswith(prefix):
        return None
    name = path.removeprefix(prefix).split("/", 1)[0]
    return prefix + name if name else None


async def server_closure(store: AsyncStore, python: str, files: Sequence[str]) -> list[StorePath]:
    """The closure of the store path that holds `python`.

    Each of `files` must resolve into it. One that does not, such as a
    package on PYTHONPATH or an editable install, raises `NotShippable`:
    the far host would start an interpreter that cannot import it."""
    store_dir = await store.store_dir()
    top = store_path_of(python, store_dir)
    if top is None:
        raise NotShippable(f"the interpreter {python} is outside {store_dir}, so no store copy carries it")
    closure = await store.compute_fs_closure([await store.parse_store_path(top)])
    held = {await store.print_store_path(path) for path in closure}
    for name in files:
        # Not the interpreter path: a venv's bin/python is a link out of the
        # venv, and the venv is what carries the packages.
        real = str(await anyio.Path(name).resolve())
        if store_path_of(real, store_dir) not in held:
            raise NotShippable(f"{real} is outside the closure of {python}, so a copy of it cannot import it")
    return closure


@contextlib.asynccontextmanager
async def shipped(
    connection: asyncssh.SSHClientConnection,
    python: str,
    *,
    files: Sequence[str] = (),
    socket: str = DAEMON_SOCKET,
    source: str | None = None,
) -> AsyncIterator[None]:
    """Copy the closure of `python` (see `server_closure`) from the
    store `source` names (Nix's `store` setting when None) to the Nix
    daemon at `socket` on the far end of `connection`, and hold the
    daemon connection open while the block runs.

    Paths the far store holds already are skipped. The SSH server must
    allow Unix socket forwarding (`AllowStreamLocalForwarding`, on by
    default in OpenSSH); `huggorm.server --ssh-listen` does not."""
    async with AsyncSession(source) as session:
        local = session.store()
        paths = await server_closure(local, python, files)
        # Mode 0700: no other user on this host reaches the far daemon as us.
        async with anyio.TemporaryDirectory(prefix="huggorm-") as here:
            near = os.path.join(here, "daemon")
            listener = await connection.forward_local_path(near, socket)
            try:
                far = session.store(f"unix://{near}")
                try:
                    await local.copy_closure(far, paths, check_sigs=False)
                    yield
                finally:
                    await far.aclose()
            finally:
                listener.close()
                await listener.wait_closed()
