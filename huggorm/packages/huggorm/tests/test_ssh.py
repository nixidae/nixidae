"""A server over an SSH session (nixidae/nixidae#551).

An asyncssh server in this process, with a generated host key, serves
`serve_stream` on each session, so both ends of `SshStream` run over
real SSH and the test stays hermetic.
"""

import socket
from collections.abc import AsyncIterator
from typing import Any

import anyio
import asyncssh
import pytest
from conftest import Server
from test_remote_eval import DUMMY, Counted, local_path
from test_source_accessor import SOURCE, Memory

from huggorm import ServerOptions, remote, worker
from huggorm.server import serve_stream
from huggorm.session import AsyncRemoteSession
from huggorm.spawn import STOP_GRACE
from huggorm.ssh import (
    Ssh,
    SshConnection,
    SshStream,
    connected,
    forwarded,
    server_command,
    socket_dialer,
    spawned,
)
from huggorm_generated import AsyncEvalState, AsyncStore


class _AnyPassword(asyncssh.SSHServer):
    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return True


@pytest.fixture
def ssh_commands() -> list[str]:
    """The command each session asked for, in order."""
    return []


@pytest.fixture
async def ssh_port(ssh_commands: list[str]) -> AsyncIterator[int]:
    """An SSH server on a free local port that serves huggorm on each
    session, whatever command the session asks for."""

    async def session(stdin: Any, stdout: Any, _stderr: Any) -> None:
        ssh_commands.append(stdin.channel.get_command())
        await serve_stream(SshStream(stdin, stdout))
        # What a remote server process does when it exits.
        stdout.channel.exit(0)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    acceptor = await asyncssh.listen(
        sock=listener,
        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
        server_factory=_AnyPassword,
        session_factory=session,
        encoding=None,
        line_editor=False,
    )
    try:
        yield listener.getsockname()[1]
    finally:
        acceptor.close()
        await acceptor.wait_closed()


@pytest.fixture
async def ssh_connection(ssh_port: int) -> AsyncIterator[asyncssh.SSHClientConnection]:
    async with asyncssh.connect(
        "127.0.0.1", ssh_port, username="test", password="test", known_hosts=None
    ) as connection:
        yield connection


async def test_a_server_over_ssh_evaluates_and_ends_with_its_session(
    ssh_connection: asyncssh.SSHClientConnection,
) -> None:
    """The stop is shielded, so the time is the measure: a server that
    did not end at EOF holds the block open for `STOP_GRACE`."""
    started = anyio.current_time()
    async with (
        spawned(ssh_connection, "huggorm") as dial,
        AsyncRemoteSession.connect(dial, store_uri="dummy://") as session,
    ):
        state = await session.eval(await session.store())
        value = await state.eval_expr("1 + 1")
        assert await value.integer() == 2
    assert anyio.current_time() - started < STOP_GRACE


async def test_connected_opens_the_connection_itself(ssh_port: int) -> None:
    async with (
        connected(
            "127.0.0.1",
            port=ssh_port,
            username="test",
            password="test",
            command="huggorm",
            known_hosts=None,
        ) as dial,
        AsyncRemoteSession.connect(dial, store_uri="dummy://") as session,
    ):
        state = await session.eval(await session.store())
        value = await state.eval_expr("1 + 1")
        assert await value.integer() == 2


_OPTIONS = ServerOptions(settings={"system": "x86_64-test"}, load_config=True)


async def test_ssh_runs_the_server_command_for_its_options(ssh_port: int, ssh_commands: list[str]) -> None:
    via = Ssh(
        "127.0.0.1",
        port=ssh_port,
        username="test",
        password="test",
        python="/remote/python",
        connect_options={"known_hosts": None},
    )
    async with worker(_OPTIONS, via=via, store_uri="dummy://") as session:
        state = await session.eval(await session.store())
        assert await (await state.eval_expr("1 + 1")).integer() == 2
    assert ssh_commands == [server_command(_OPTIONS, python="/remote/python")]


async def test_an_ssh_connection_the_caller_holds_carries_a_worker(
    ssh_connection: asyncssh.SSHClientConnection, ssh_commands: list[str]
) -> None:
    via = SshConnection(ssh_connection)
    async with worker(_OPTIONS, via=via, store_uri="dummy://") as session:
        state = await session.eval(await session.store())
        assert await (await state.eval_expr("1 + 1")).integer() == 2
    assert ssh_commands == [server_command(_OPTIONS)]
    assert not ssh_connection.is_closed()


# -- a server that already runs, reached over Unix socket forwarding ------


class _Forwarding(_AnyPassword):
    """Forwards a channel to `allowed` only, as an sshd that allows one
    socket would."""

    def __init__(self, allowed: str) -> None:
        self.allowed = allowed

    def unix_connection_requested(self, dest_path: str) -> bool:
        return dest_path == self.allowed


@pytest.fixture
async def forwarding_port(callback_server: Server) -> AsyncIterator[int]:
    """An SSH server on a free local port that forwards channels to the
    callback server's socket."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    acceptor = await asyncssh.listen(
        sock=listener,
        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
        server_factory=lambda: _Forwarding(str(callback_server.path)),
        encoding=None,
    )
    try:
        yield listener.getsockname()[1]
    finally:
        acceptor.close()
        await acceptor.wait_closed()


def _dialer(callback_server: Server, port: int) -> Any:
    return socket_dialer(
        "127.0.0.1", str(callback_server.path), port=port, username="t", password="t", known_hosts=None
    )


async def test_a_running_server_serves_two_connections_over_one_ssh_link(
    callback_server: Server, forwarding_port: int
) -> None:
    """The second huggorm connection claims the state the first one
    detached: the server stays warm between them."""
    async with asyncssh.connect(
        "127.0.0.1", forwarding_port, username="t", password="t", known_hosts=None
    ) as connection:
        dial = forwarded(connection, str(callback_server.path))
        async with remote.connect(dial) as first:
            state = await first.acquire(AsyncEvalState, await first.acquire(AsyncStore, DUMMY))
            await state.eval_expr("let x = 40 + 2; in x")
            assert await first.detach(all=True)
            token, state_id = first.token, remote.handle_of(state)
        async with remote.connect(dial, claim=token) as second:
            claimed = second.proxy(AsyncEvalState, state_id)
            assert await (await claimed.eval_expr("1 + 1")).integer() == 2


async def test_a_client_claims_its_state_over_a_new_ssh_link(callback_server: Server, forwarding_port: int) -> None:
    """The first link drops after the client detached. The next dial
    opens a new SSH connection; the client claims its state, mounts its
    tree again, and the server reads the files the first accessor never
    served through the new one."""
    dial = _dialer(callback_server, forwarding_port)
    path = local_path(SOURCE)
    async with remote.connect(dial, experimental_callbacks=True) as first:
        store = await first.acquire(AsyncStore, DUMMY)
        state = await first.acquire(AsyncEvalState, store)
        root = await store.print_store_path(await (await state.mount(Memory(SOURCE), "source", path)).path())
        with anyio.fail_after(30):
            assert await (await state.eval_file(f"{root}/sub/x.nix")).integer() == 42
        assert await first.detach(all=True)
        token, state_id = first.token, remote.handle_of(state)
    fresh = Counted(SOURCE)
    async with remote.connect(dial, claim=token, experimental_callbacks=True) as second:
        claimed = second.proxy(AsyncEvalState, state_id)
        with anyio.fail_after(30):
            await claimed.mount(fresh, "source", path)
            assert await (await claimed.eval_expr(f"(import {root}/default.nix).joined")).string_value() == "hello\n"
    assert "/default.nix" in fresh.reads and "/sub/x.nix" not in fresh.reads


async def test_a_socket_the_ssh_server_refuses_raises_connection_refused(
    callback_server: Server, forwarding_port: int
) -> None:
    dial = socket_dialer(
        "127.0.0.1", "/nowhere.sock", port=forwarding_port, username="t", password="t", known_hosts=None
    )
    with anyio.fail_after(30), pytest.raises(ConnectionRefusedError, match=r"/nowhere\.sock"):
        await dial()
