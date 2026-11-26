"""A server over an SSH session (nixidae/nixidae#551).

An asyncssh server in this process, with a generated host key, serves
`serve_stream` on each session, so both ends of `SshStream` run over
real SSH and the test stays hermetic.
"""

import functools
import json
import os
import pathlib
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import anyio
import asyncssh
import pytest
from conftest import Server
from test_remote_eval import DUMMY, Counted, local_path
from test_source_accessor import SOURCE, Memory

from huggorm import ServerOptions, remote, transport, worker
from huggorm.server import serve, serve_stream
from huggorm.server_options import SshListen
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

    # Any key: the OpenSSH client runs in batch mode and offers no password.
    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        return True


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


# -- the same server through the OpenSSH client ---------------------------


@pytest.fixture
def ssh_key(tmp_path: pathlib.Path) -> pathlib.Path:
    key = tmp_path / "id_ed25519"
    asyncssh.generate_private_key("ssh-ed25519").write_private_key(str(key))
    key.chmod(0o600)
    return key


def _openssh(path: str, port: int, key: pathlib.Path) -> Any:
    # Any: huggorm.transport.Dialer, a callable alias.
    return transport.openssh(
        "t@127.0.0.1",
        path,
        ssh=os.environ.get("HUGGORM_TEST_SSH", "ssh"),
        options=[
            "-p",
            str(port),
            "-i",
            str(key),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
        ],
    )


async def test_openssh_reaches_a_running_server_and_redials(
    callback_server: Server, forwarding_port: int, ssh_key: pathlib.Path
) -> None:
    """One `ssh -W` per dial: the second connection claims the state the
    first detached."""
    dial = _openssh(str(callback_server.path), forwarding_port, ssh_key)
    with anyio.fail_after(60):
        async with remote.connect(dial) as first:
            state = await first.acquire(AsyncEvalState, await first.acquire(AsyncStore, DUMMY))
            assert await (await state.eval_expr("40 + 2")).integer() == 42
            assert await first.detach(all=True)
            token, state_id = first.token, remote.handle_of(state)
        async with remote.connect(dial, claim=token) as second:
            assert await (await second.proxy(AsyncEvalState, state_id).eval_expr("1 + 1")).integer() == 2


async def test_openssh_names_a_socket_the_server_refuses(
    callback_server: Server, forwarding_port: int, ssh_key: pathlib.Path
) -> None:
    dial = _openssh("/nowhere.sock", forwarding_port, ssh_key)
    with anyio.fail_after(60), pytest.raises(transport.SshFailed) as caught:
        async with remote.connect(dial):
            pass
    assert caught.value.returncode == 255
    assert "forwarding failed" in caught.value.stderr or "open failed" in caught.value.stderr


async def test_openssh_names_a_host_that_does_not_answer(ssh_key: pathlib.Path) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    dial = _openssh("/any.sock", port, ssh_key)
    with anyio.fail_after(60), pytest.raises(transport.SshFailed, match="refused"):
        async with remote.connect(dial):
            pass


# -- huggorm.server listening on SSH itself --------------------------------


@dataclass(frozen=True)
class _Listening:
    path: pathlib.Path
    port: int
    key: asyncssh.SSHKey
    authorized: pathlib.Path


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@pytest.fixture
async def listening(tmp_path: pathlib.Path) -> AsyncIterator[_Listening]:
    """`serve` on a Unix socket and on SSH, one dispatcher, in this
    process. The client key is the one `authorized_keys` lists."""
    key = asyncssh.generate_private_key("ssh-ed25519")
    authorized = tmp_path / "authorized_keys"
    authorized.write_bytes(key.export_public_key())
    path = tmp_path / "server.sock"
    port = _free_port()
    listen = SshListen("127.0.0.1", port, tmp_path / "host_key", authorized)
    async with anyio.create_task_group() as tg:
        await tg.start(functools.partial(serve, str(path), callbacks=True, ssh=listen))
        yield _Listening(path, port, key, authorized)
        tg.cancel_scope.cancel()


def _over_ssh(at: _Listening, key: asyncssh.SSHKey | None = None) -> Any:
    # Any: an async context manager over a huggorm.transport.Dialer.
    return connected(
        "127.0.0.1",
        port=at.port,
        username="t",
        command="huggorm",
        client_keys=[key or at.key],
        known_hosts=None,
    )


async def test_the_server_listens_on_ssh_itself(listening: _Listening) -> None:
    with anyio.fail_after(60):
        async with _over_ssh(listening) as dial, remote.connect(dial) as client:
            state = await client.acquire(AsyncEvalState, await client.acquire(AsyncStore, DUMMY))
            assert await (await state.eval_expr("40 + 2")).integer() == 42


async def test_the_ssh_listener_refuses_a_key_it_does_not_list(listening: _Listening) -> None:
    stranger = asyncssh.generate_private_key("ssh-ed25519")
    with anyio.fail_after(60), pytest.raises(asyncssh.PermissionDenied):
        async with _over_ssh(listening, stranger):
            pass


async def test_the_ssh_listener_admits_a_key_added_while_it_runs(listening: _Listening) -> None:
    later = asyncssh.generate_private_key("ssh-ed25519")
    with anyio.fail_after(60):
        authorized = anyio.Path(listening.authorized)
        await authorized.write_bytes(await authorized.read_bytes() + later.export_public_key())
        async with _over_ssh(listening, later) as dial, remote.connect(dial) as client:
            state = await client.acquire(AsyncEvalState, await client.acquire(AsyncStore, DUMMY))
            assert await (await state.eval_expr("1")).integer() == 1


async def test_the_ssh_listener_admits_nobody_without_authorized_keys(listening: _Listening) -> None:
    await anyio.Path(listening.authorized).unlink()
    with anyio.fail_after(60), pytest.raises(asyncssh.PermissionDenied):
        async with _over_ssh(listening):
            pass


async def test_a_state_detached_on_the_socket_is_claimed_over_ssh(listening: _Listening) -> None:
    """One dispatcher serves both listeners, so the token is good on
    either."""
    with anyio.fail_after(60):
        async with remote.connect(listening.path, experimental_callbacks=True) as local:
            store = await local.acquire(AsyncStore, DUMMY)
            state = await local.acquire(AsyncEvalState, store)
            mounted = await state.mount(Memory(SOURCE), "source", local_path(SOURCE))
            root = await store.print_store_path(await mounted.path())
            assert await (await state.eval_file(f"{root}/sub/x.nix")).integer() == 42
            assert await local.detach(all=True)
            token, state_id = local.token, remote.handle_of(state)
        fresh = Counted(SOURCE)
        async with (
            _over_ssh(listening) as dial,
            remote.connect(dial, claim=token, experimental_callbacks=True) as far,
        ):
            claimed = far.proxy(AsyncEvalState, state_id)
            await claimed.mount(fresh, "source", local_path(SOURCE))
            got = json.loads(await (await claimed.eval_file(f"{root}/default.nix")).to_json())
    assert got["imported"] == 42
    assert "/sub/x.nix" not in fresh.reads
