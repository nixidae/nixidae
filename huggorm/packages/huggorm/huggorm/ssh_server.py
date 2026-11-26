"""
`huggorm.server` listening on SSH itself, through asyncssh (nixidae/nixidae#738).

No sshd on the server host: the server holds its host key and the
`authorized_keys` it admits, and each SSH session's stdin and stdout
are one connection, served by the same dispatcher as the server's
Unix socket. So one warm server takes clients from both, and a client
that reconnects over SSH claims the state it detached.

A client reaches it with `huggorm.ssh.connected(host, command="")` or
any SSH client that runs a session; the command a session asks for is
not read. Needs the `ssh` extra.
"""

from __future__ import annotations

import logging
import pathlib
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import anyio.abc
import asyncssh

from .server_options import SshListen
from .ssh import SshStream

logger = logging.getLogger(__name__)

#: A silent client is dropped after 3 x 10 s, so its connection ends and
#: its leases wait for a claim instead of a dead peer.
KEEPALIVE_INTERVAL = 10
KEEPALIVE_COUNT_MAX = 3

Serve = Callable[..., Awaitable[None]]


def load_host_key(path: pathlib.Path) -> asyncssh.SSHKey:
    """The key at `path`, or a new ed25519 key written there."""
    if path.exists():
        return asyncssh.read_private_key(str(path))
    key = asyncssh.generate_private_key("ssh-ed25519")
    key.write_private_key(str(path))
    path.chmod(0o600)
    logger.info("generated an SSH host key at %s", path)
    return key


async def serve_ssh(
    connection: Serve,
    listen: SshListen,
    *,
    task_status: anyio.abc.TaskStatus[int] = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Serve SSH sessions through `connection` (a dispatcher's) until
    cancelled. Reports the bound port, which `listen.port` 0 leaves to
    the kernel.

    asyncssh runs each session on a task of its own. The session hands
    its stream to a task in this function's group and waits for it, so
    every connection belongs to a scope this server owns."""
    host_key = await anyio.to_thread.run_sync(load_host_key, listen.host_key)
    async with anyio.create_task_group() as tg:

        async def session(process: Any) -> None:
            # Any: asyncssh.SSHServerProcess[bytes], typed loosely by asyncssh.
            done = anyio.Event()

            async def run() -> None:
                try:
                    await connection(SshStream(process.stdin, process.stdout), trusted=True)
                finally:
                    done.set()

            tg.start_soon(run)
            await done.wait()
            process.exit(0)

        acceptor = await asyncssh.listen(
            listen.host,
            listen.port,
            server_host_keys=[host_key],
            authorized_client_keys=str(listen.authorized_keys),
            process_factory=session,
            encoding=None,
            keepalive_interval=KEEPALIVE_INTERVAL,
            keepalive_count_max=KEEPALIVE_COUNT_MAX,
        )
        try:
            port = acceptor.get_port()
            logger.info("listening on ssh %s:%s", listen.host, port)
            task_status.started(port)
            await anyio.sleep_forever()
        finally:
            with anyio.CancelScope(shield=True):
                acceptor.close()
                with anyio.move_on_after(5):
                    await acceptor.wait_closed()
                tg.cancel_scope.cancel()
