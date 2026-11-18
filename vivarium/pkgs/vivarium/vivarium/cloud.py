"""The host's half of vivarium's cloud API: instances of templates, launched by guests. nixidae/nixidae#1408.

`vivarium_agent/cloud.py` is the wire. Here a launch is recorded, answered
`pending` at once, and booted in the background, as a cloud does. A phase
plays the provider's side through `vms.cloud`: `capacity`, `boot_delay`,
`interrupt`.

The API has no limits of its own. A template with no `capacity` entry
launches without bound, and every guest of the run may list, describe and
terminate every instance, not only the ones it launched.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import anyio
from vivarium_agent.arpyc import Service
from vivarium_runner.held import Held, hold

if TYPE_CHECKING:
    from vivarium_runner import Machine

    from .session import Session


class State(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    TERMINATED = "terminated"
    FAILED = "failed"


LIVE = frozenset({State.PENDING, State.RUNNING})


@dataclass(slots=True)
class Instance:
    id: str
    template: str
    tags: dict[str, str]
    user_data: str | None
    launched_by: str | None
    """The guest that asked, or None for a phase."""
    state: State = State.PENDING
    error: str | None = None
    notice: float | None = None
    """When an interruption ends this instance, in seconds since the epoch, as a spot notice says."""
    settled: anyio.Event = field(default_factory=anyio.Event)
    """Set once the state leaves `pending`."""
    ended: anyio.Event = field(default_factory=anyio.Event)
    """Set once the instance is terminated, or failed after it ran."""

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "template": self.template,
            "state": str(self.state),
            "tags": dict(self.tags),
            "launched_by": self.launched_by,
            "error": self.error,
            "notice": self.notice,
        }


def matches(instance: Instance, tags: Mapping[str, str]) -> bool:
    return all(instance.tags.get(key) == value for key, value in tags.items())


@dataclass
class Cloud:
    """Every instance of the run, and the provider's knobs a phase turns."""

    session: Session
    capacity: dict[str, int] = field(default_factory=dict)
    """Live instances a template may have; one past it answers 409. No entry is no limit."""
    boot_delay: float = 0.0
    """Seconds a launch stays `pending` before its guest boots: a cloud's scheduling time."""
    instances: dict[str, Instance] = field(default_factory=dict)
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    # In the session's own group: an instance outlives the request that
    # launched it and the phase that runs. Stopped in `close`.
    _tasks: dict[str, Held] = field(default_factory=dict)
    _ending: set[str] = field(default_factory=set)

    async def handle(self, caller: str | None, op: str, args: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """Answer one API call from guest *caller*: an HTTP status and a body."""
        if op == "launch":
            return await self._launch(caller, args)
        if op == "list":
            tags = args.get("tags", {})
            return HTTPStatus.OK, {"instances": [i.view() for i in self.instances.values() if matches(i, tags)]}
        if op == "self":
            own = self.instances.get(caller or "")
            if own is None:
                return HTTPStatus.NOT_FOUND, {"error": f"{caller} is a node of the run, not an instance"}
            return HTTPStatus.OK, own.view()
        instance = self.instances.get(str(args.get("id")))
        if instance is None:
            return HTTPStatus.NOT_FOUND, {"error": f"no instance {args.get('id')!r}"}
        if op == "describe":
            return HTTPStatus.OK, instance.view()
        if op == "terminate":
            await self.terminate(instance.id)
            return HTTPStatus.ACCEPTED, instance.view()
        return HTTPStatus.NOT_FOUND, {"error": f"no operation {op!r}"}

    async def _launch(self, caller: str | None, args: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        template = str(args["template"])
        if template not in self.session.templates:
            known = ", ".join(self.session.templates) or "none"
            return HTTPStatus.NOT_FOUND, {"error": f"no template {template!r}; the run has {known}"}
        live = sum(1 for i in self.instances.values() if i.template == template and i.state in LIVE)
        limit = self.capacity.get(template)
        if limit is not None and live >= limit:
            return HTTPStatus.CONFLICT, {"error": f"insufficient capacity: {template} has {live} of {limit}"}
        instance = self.launch(template, args.get("tags") or {}, args.get("user_data"), caller)
        return HTTPStatus.ACCEPTED, instance.view()

    def launch(
        self, template: str, tags: Mapping[str, str], user_data: str | None, caller: str | None = None
    ) -> Instance:
        """Record an instance and boot it in the background; it is `pending` until its guest is up."""
        instance = Instance(
            id=f"i-{next(self._ids):04d}",
            template=template,
            tags={str(k): str(v) for k, v in tags.items()},
            user_data=None if user_data is None else str(user_data),
            launched_by=caller,
        )
        self.instances[instance.id] = instance
        self._tasks[instance.id] = hold(self.session.tasks, self._boot, instance)
        return instance

    async def _boot(self, instance: Instance) -> None:
        """Boot *instance*, then hold until its guest stops by itself, which ends it.

        A guest's own poweroff ends its instance, as EC2's
        `InstanceInitiatedShutdownBehavior=terminate` does: a builder that
        powers itself off must read as gone, not as running.
        """
        try:
            await anyio.sleep(self.boot_delay)
            vm = await self.session.launch(instance.template, instance.id, instance.user_data)
        except anyio.get_cancelled_exc_class():
            self._tasks.pop(instance.id, None)
            instance.settled.set()
            raise
        except Exception as error:  # noqa: BLE001 -- the instance carries any failure as its state
            instance.state = State.FAILED
            instance.error = str(error)
            self._tasks.pop(instance.id, None)
            instance.settled.set()
            return
        instance.state = State.RUNNING
        instance.settled.set()
        code = await vm.exited()
        # Out of `_tasks` first: `_end` cancels the boot task it finds there, and this task is that one.
        self._tasks.pop(instance.id, None)
        if code == 0:
            await self._end(instance.id, State.TERMINATED)
        else:
            await self._end(instance.id, State.FAILED, f"the guest's process ended with exit {code}")

    async def wait_running(self, instance_id: str, timeout: float = 300) -> Machine:
        """The guest of *instance_id* once it runs; a failed launch raises its error."""
        instance = self.instances[instance_id]
        try:
            with anyio.fail_after(timeout):
                await instance.settled.wait()
        except TimeoutError:
            raise RuntimeError(f"{instance_id} still {instance.state} after {timeout:g} s") from None
        if instance.state is not State.RUNNING:
            raise RuntimeError(f"{instance_id} is {instance.state}: {instance.error}")
        return self.session.vms_up[instance_id]

    async def terminate(self, instance_id: str) -> None:
        """End an instance whatever its state; a pending one never boots.

        Once: a second call, such as an API delete while the notice ends the
        instance, waits for the first. Two reached `Session.terminate`, and
        the second raised KeyError for a guest the first had removed.
        """
        await self._end(instance_id, State.TERMINATED)

    async def _end(self, instance_id: str, state: State, error: str | None = None) -> None:
        """`terminate`, ending in *state*: a guest that crashed by itself ends `failed`."""
        instance = self.instances[instance_id]
        if instance_id in self._ending or instance.ended.is_set():
            await instance.ended.wait()
            return
        self._ending.add(instance_id)
        try:
            # The notice's own task takes itself out first, so this never cancels the caller.
            notice = self._tasks.pop(f"{instance_id}/notice", None)
            if notice is not None:
                notice.scope.cancel()
            task = self._tasks.pop(instance_id, None)
            if task is not None:
                await task.stop()
            # A boot cancelled partway may hold ports and a `Machine` already.
            if self.session.holds(instance_id):
                await self.session.terminate(instance_id)
            instance.state = state
            if error is not None:
                instance.error = error
            instance.settled.set()
            instance.ended.set()
        finally:
            self._ending.discard(instance_id)

    async def wait_ended(self, instance_id: str, timeout: float = 300) -> None:
        """Until *instance_id* ends: by a call, a phase, the end of its notice, or its guest's own stop."""
        try:
            with anyio.fail_after(timeout):
                await self.instances[instance_id].ended.wait()
        except TimeoutError:
            raise RuntimeError(f"{instance_id} still {self.instances[instance_id].state} after {timeout:g} s") from None

    async def interrupt(self, instance_id: str, seconds: float) -> None:
        """Give *instance_id* a notice, as a spot interruption does, and terminate it when the notice ends."""
        instance = self.instances[instance_id]
        instance.notice = time.time() + seconds

        async def end() -> None:
            await anyio.sleep(seconds)
            self._tasks.pop(f"{instance_id}/notice", None)
            await self.terminate(instance_id)

        self._tasks[f"{instance_id}/notice"] = hold(self.session.tasks, end)

    async def close(self) -> None:
        tasks, self._tasks = list(self._tasks.values()), {}
        for task in tasks:
            task.scope.cancel()
        for task in tasks:
            await task.wait()


class CloudService(Service):
    """Answers one guest's calls over its agent channel."""

    def __init__(self, cloud: Cloud, guest: str) -> None:
        self.cloud = cloud
        self.guest = guest

    async def exposed_cloud(self, op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        status, body = await self.cloud.handle(self.guest, op, args)
        # brine dumps an int, not an IntEnum such as HTTPStatus.
        return int(status), body
