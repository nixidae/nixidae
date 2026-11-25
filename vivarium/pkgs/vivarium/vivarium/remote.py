"""Each guest's `Machine`, for a process other than the run's.

A pytest that drives vivarium from outside (nixidae/nixidae#1413) holds
no `Session`: the run is its own process, in its own user namespace, and
the test reaches its guests through `SOCKET`. Each connection is arpyc,
the agent channel's protocol, so a call is one round trip. A test holds a
`RemoteMachine` for each guest:

    out = (await vm.succeed("hostname")).stdout
    ip = await vm.get("uplink_ip")

`RemoteMachine` is the wire API: the socket serves only the methods it
declares (`CALLS`) and the attributes `get` names (`READABLE`). Each
method takes `Machine`'s own signature, so a rename fails the type check
on both sides. Arguments and results are plain data, and a list arrives
as a tuple. A `CommandResult` crosses as a tuple and arrives rebuilt.

The socket is 0600, as the control socket is, and exists only where the
control socket does: never in a sandboxed check.
"""

from __future__ import annotations

import dataclasses
import functools
import inspect
import os
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, Concatenate, Final, Literal, get_args

import anyio
from anyio.abc import SocketAttribute
from vivarium_agent.arpyc import AsyncConnection, Service, listen
from vivarium_runner.control import CommandResult
from vivarium_runner.machine import Machine, MachineError

from . import places
from .control import reachable

if TYPE_CHECKING:
    from anyio.abc import SocketStream, TaskStatus

    from .session import Session

SOCKET: Final = "machines.sock"

type Readable = Literal["name", "ip", "uplink_ip", "control_kind", "can_resize", "screen_size"]
"""The attributes and properties `RemoteMachine.get` reads."""

READABLE: Final = frozenset(get_args(Readable.__value__))


_calls: set[str] = set()


def _remote[**P, R](
    method: Callable[Concatenate[Machine, P], Awaitable[R]],
) -> Callable[Concatenate[RemoteMachine, P], Coroutine[Any, Any, R]]:
    """`method` of the guest's `Machine`, as one round trip."""
    name = _listed(method)

    @functools.wraps(method)
    async def call(self: RemoteMachine, *args: P.args, **kwargs: P.kwargs) -> R:
        result: R = await self.conn.call("call", (self.name, name, args, kwargs), {})
        return result

    return call


def _remote_plain[**P, R](
    method: Callable[Concatenate[Machine, P], R],
) -> Callable[Concatenate[RemoteMachine, P], Coroutine[Any, Any, R]]:
    """`_remote` for a method that is not a coroutine on the run's side."""
    name = _listed(method)

    @functools.wraps(method)
    async def call(self: RemoteMachine, *args: P.args, **kwargs: P.kwargs) -> R:
        result: R = await self.conn.call("call", (self.name, name, args, kwargs), {})
        return result

    return call


def _remote_result[**P](
    method: Callable[Concatenate[Machine, P], Awaitable[CommandResult]],
) -> Callable[Concatenate[RemoteMachine, P], Coroutine[Any, Any, CommandResult]]:
    """`_remote` for a method that returns a `CommandResult`."""
    name = _listed(method)

    @functools.wraps(method)
    async def call(self: RemoteMachine, *args: P.args, **kwargs: P.kwargs) -> CommandResult:
        returncode, stdout, stderr = await self.conn.call("call", (self.name, name, args, kwargs), {})
        return CommandResult(returncode, stdout, stderr)

    return call


def _listed(method: Callable[..., object]) -> str:
    _calls.add(method.__name__)
    return method.__name__


class RemoteMachine:
    """A guest of another process's run. The test side of `machines.sock`."""

    def __init__(self, conn: AsyncConnection, name: str) -> None:
        self.conn = conn
        self.name = name

    execute = _remote_result(Machine.execute)
    succeed = _remote_result(Machine.succeed)
    fail = _remote_result(Machine.fail)
    switch_to = _remote_result(Machine.switch_to)
    add_closure = _remote(Machine.add_closure)
    add_paths = _remote(Machine.add_paths)
    unit_state = _remote(Machine.unit_state)
    unit_info = _remote(Machine.unit_info)
    listening = _remote(Machine.listening)
    relay = _remote(Machine.relay)
    count_processes = _remote(Machine.count_processes)
    journal = _remote(Machine.journal)
    meminfo = _remote(Machine.meminfo)
    host_memory_kib = _remote_plain(Machine.host_memory_kib)
    drop_caches = _remote(Machine.drop_caches)
    shrink = _remote(Machine.shrink)
    grow = _remote(Machine.grow)
    console_tail = _remote_plain(Machine.console_tail)
    wait_for_console_text = _remote(Machine.wait_for_console_text)
    wait_for_unit = _remote(Machine.wait_for_unit)
    wait_until_succeeds = _remote_result(Machine.wait_until_succeeds)
    wait_until_fails = _remote_result(Machine.wait_until_fails)
    screen_text = _remote(Machine.screen_text)
    send_key = _remote(Machine.send_key)
    send_chars = _remote(Machine.send_chars)
    move = _remote(Machine.move)
    press = _remote(Machine.press)
    release = _remote(Machine.release)
    click = _remote(Machine.click)
    drag = _remote(Machine.drag)
    scroll = _remote(Machine.scroll)

    async def get(self, attribute: Readable) -> Any:  # noqa: ANN401 -- the attribute's own type
        """A plain attribute or property, such as `uplink_ip`."""
        return await self.conn.call("get", (self.name, attribute), {})

    async def artifacts(self) -> str | None:
        """The host directory that is the guest's `/artifacts`."""
        found: str | None = await self.conn.call("artifacts", (self.name,), {})
        return found

    async def until[T](
        self,
        what: str,
        check: Callable[[], Awaitable[T | None]],
        timeout: float = 120,
        poll: float = 1.0,
        evidence: Callable[[], Awaitable[str]] | None = None,
    ) -> T:
        """`Machine.until`, with *check* run in the test: a function does
        not cross the socket."""
        try:
            with anyio.fail_after(timeout):
                while True:
                    value = await check()
                    if value is not None:
                        return value
                    await anyio.sleep(poll)
        except TimeoutError:
            seen = f":\n{await evidence()}" if evidence is not None else ""
            raise MachineError(f"[{self.name}] {what}: not reached within {timeout:.0f}s{seen}") from None

    async def never(
        self,
        what: str,
        check: Callable[[], Awaitable[object | None]],
        timeout: float,
        poll: float = 1.0,
    ) -> None:
        """`Machine.never`, with *check* run in the test."""
        started = time.monotonic()
        with anyio.move_on_after(timeout):
            while True:
                value = await check()
                if value is not None:
                    raise MachineError(
                        f"[{self.name}] {what}: happened after {time.monotonic() - started:.0f}s:\n{value}"
                    )
                await anyio.sleep(poll)

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 -- never returns
        if name in READABLE:
            raise AttributeError(f"{name} is the guest's: read it with `await vm.get({name!r})`")
        if hasattr(Machine, name) and not name.startswith("_"):
            raise AttributeError(f"Machine.{name} does not cross machines.sock; see vivarium.remote.CALLS")
        raise AttributeError(name)

    def __repr__(self) -> str:
        return f"<RemoteMachine {self.name}>"


CALLS: Final = frozenset(_calls)
"""The `Machine` methods `machines.sock` calls: those `RemoteMachine` declares."""


class MachinesService(Service):
    """`machines`, `call`, `get` and `artifacts` on the run's guests that are up."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def exposed_machines(self) -> tuple[str, ...]:
        return tuple(self.session.vms_up)

    def exposed_get(self, machine: str, name: str) -> Any:  # noqa: ANN401 -- one of READABLE
        if name not in READABLE:
            raise AttributeError(
                f"{name} is not readable through machines.sock; these are: {', '.join(sorted(READABLE))}"
            )
        return getattr(self.session.vms_up[machine], name)

    def exposed_artifacts(self, machine: str) -> str | None:
        """A `Path` does not cross the wire, so `get` cannot carry it."""
        path = self.session.vms_up[machine].artifacts
        return None if path is None else str(path)

    async def exposed_call(
        self,
        machine: str,
        method: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> Any:  # noqa: ANN401 -- what the method returns
        if method not in CALLS:
            raise AttributeError(f"Machine.{method} is not served by machines.sock")
        result = getattr(self.session.vms_up[machine], method)(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, CommandResult):
            return dataclasses.astuple(result)
        return result


async def _client(stream: SocketStream, service: MachinesService) -> None:
    # arpyc reads a raw fd from the loop; the duplicate outlives the stream.
    fd = os.dup(stream.extra(SocketAttribute.raw_socket).fileno())
    await stream.aclose()
    conn = listen(fd, service, raw_tty=False)
    await conn.serve_forever()


async def serve(session: Session, *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
    """Listen until cancelled, beside the control socket."""
    path = places.prepare(session.out) / SOCKET
    path.unlink(missing_ok=True)
    with reachable(path) as name:
        listener = await anyio.create_unix_listener(name)
    path.chmod(0o600)
    service = MachinesService(session)
    task_status.started()
    try:
        await listener.serve(lambda stream: _client(stream, service))
    finally:
        with anyio.CancelScope(shield=True):
            await listener.aclose()
        path.unlink(missing_ok=True)
