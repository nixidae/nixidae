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

from huggorm import ServerOptions, worker
from huggorm.server import serve_stream
from huggorm.session import AsyncRemoteSession
from huggorm.spawn import STOP_GRACE
from huggorm.ssh import Ssh, SshConnection, SshStream, connected, server_command, spawned


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
