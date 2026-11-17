"""Each guest's `Machine`, for a process other than the run's.

A pytest that drives vivarium from outside (nixidae/nixidae#1413) holds
no `Session`: the run is its own process, in its own user namespace, and
the test reaches its guests through `SOCKET`. Each connection is arpyc,
the agent channel's protocol, so a call is one round trip:

    names = await conn.call("machines", (), {})
    out = await conn.call("call", ("a", "succeed", ("hostname",), {}), {})

The socket is 0600, as the control socket is, and exists only where the
control socket does: never in a sandboxed check.
"""

from __future__ import annotations

import inspect
import os
from typing import TYPE_CHECKING, Any, Final

import anyio
from anyio.abc import SocketAttribute
from vivarium_runner.arpyc import Service, listen

from .control import reachable

if TYPE_CHECKING:
    from anyio.abc import SocketStream, TaskStatus

    from .session import Session

SOCKET: Final = "machines.sock"


class Machines(Service):
    """`machines`, `call` and `get` on the run's guests that are up."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def exposed_machines(self) -> tuple[str, ...]:
        return tuple(self.session.vms_up)

    def _attribute(self, machine: str, name: str) -> Any:  # noqa: ANN401 -- any attribute of a Machine
        if name.startswith("_"):
            raise AttributeError(f"{name} is private to the run")
        return getattr(self.session.vms_up[machine], name)

    def exposed_get(self, machine: str, name: str) -> Any:  # noqa: ANN401 -- any attribute of a Machine
        return self._attribute(machine, name)

    def exposed_artifacts(self, machine: str) -> str | None:
        """The host directory that is the guest's `/artifacts`. A `Path`
        does not cross the wire, so `get` cannot carry it."""
        path = self.session.vms_up[machine].artifacts
        return None if path is None else str(path)

    async def exposed_call(
        self,
        machine: str,
        method: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> Any:  # noqa: ANN401 -- what the method returns
        result = self._attribute(machine, method)(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result


async def _client(stream: SocketStream, service: Machines) -> None:
    # arpyc reads a raw fd from the loop; the duplicate outlives the stream.
    fd = os.dup(stream.extra(SocketAttribute.raw_socket).fileno())
    await stream.aclose()
    conn = listen(fd, service, raw_tty=False)
    await conn.serve_forever()


async def serve(session: Session, *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
    """Listen until cancelled, beside the control socket."""
    path = session.out / SOCKET
    path.unlink(missing_ok=True)
    with reachable(path) as name:
        listener = await anyio.create_unix_listener(name)
    path.chmod(0o600)
    service = Machines(session)
    task_status.started()
    try:
        await listener.serve(lambda stream: _client(stream, service))
    finally:
        with anyio.CancelScope(shield=True):
            await listener.aclose()
        path.unlink(missing_ok=True)
