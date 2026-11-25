"""The guests of one run on their segments, booted when asked.

The mechanism `vivarium.Session` drives, with no phases and no events: a
caller with guest specs plugs them into their segments, boots the ones it
needs, launches an instance from a template while the run goes, and tears
everything down. What the run says goes through the callbacks it is given.
"""

from __future__ import annotations

import contextlib
import shutil
from collections import defaultdict
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

import anyio

from .instances import InstanceError, free_index, instance_spec
from .machine import MachineError
from .machines import Machines
from .net import build_lans

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
    from pathlib import Path

    from .machine import Machine
    from .machine_spec import MachineSpec
    from .net import Lan


class RunError(RuntimeError):
    """A request the run cannot serve: a guest it does not have, a step out of order."""


@dataclass(frozen=True, slots=True)
class Segment:
    """What the spec says of one segment beyond its members."""

    hub: bool = False
    """Frames pass through the runner's hub even with two members."""
    capture: bool = False
    """The hub writes the segment's frames to `<pcap>/<name>.pcap` from boot."""
    instance_addresses: tuple[str, ...] = ()
    """The first address an instance takes on this segment; the next instance takes the one after."""


type MachineFactory = Callable[[MachineSpec, dict[str, int] | None], Machine]
"""Makes one guest's `Machine` from its spec and its segment ends; the caller
adds what it needs before the guest boots."""


def required(names: Iterable[str], requires: Mapping[str, Sequence[str]]) -> frozenset[str]:
    """*names* and every guest they name in `vivarium.requires`, transitively."""
    found: set[str] = set()
    todo = list(names)
    while todo:
        name = todo.pop()
        if name not in found:
            found.add(name)
            todo.extend(requires.get(name, ()))
    return frozenset(found)


def segment_members(specs: Iterable[MachineSpec]) -> dict[str, list[str]]:
    """Each segment's members, `<guest>/<interface>`: neither name can hold a slash."""
    segments: dict[str, list[str]] = defaultdict(list)
    for one in specs:
        # A guest whose uplink joins a segment -- the router's port, or a
        # management network (nixidae/nixidae#958): vec0 is a member.
        if one.uplink is not None:
            segments[one.uplink].append(f"{one.name}/vec0")
        for nic in one.interfaces:
            segments[nic.segment].append(f"{one.name}/{nic.name}")
    return dict(segments)


async def shut_down(machines: Iterable[Machine], on_error: Callable[[str, Exception], None], *, kill: bool) -> None:
    """Power off every guest at once. A guest that will not stop cleanly is
    not a reason to leave the others running, so its error goes to *on_error*."""

    async def one(machine: Machine) -> None:
        try:
            await machine.shutdown(kill=kill)
        except Exception as error:  # noqa: BLE001 -- reported, and the other guests still go down
            on_error(machine.name, error)

    async with anyio.create_task_group() as group:
        for machine in machines:
            group.start_soon(one, machine)


async def _start_all(machines: Iterable[Machine]) -> dict[str, BaseException]:
    """Start every guest, and let each settle even if another fails.

    A task group cancels its siblings when one raises, which would leave
    a guest half-spawned for the teardown to trip over. So each failure
    is caught where it happens and reported after all of them are done.
    """
    failures: dict[str, BaseException] = {}

    async def start(machine: Machine) -> None:
        try:
            await machine.start()
        except BaseException as error:  # noqa: BLE001 -- recorded, then re-raised by the caller
            failures[machine.name] = error

    async with anyio.create_task_group() as group:
        for machine in machines:
            group.start_soon(start, machine)
    return failures


def _ignore(*_: object) -> None:
    pass


class Run:
    """The guests of one run: the nodes, the templates, and each instance launched from one."""

    def __init__(
        self,
        guests: Sequence[MachineSpec],
        templates: Sequence[MachineSpec],
        *,
        machine: MachineFactory,
        on_stop_error: Callable[[str, Exception], None],
        segments: Mapping[str, Segment] | None = None,
        pcap: Path | None = None,
        on_segment: Callable[[str], None] = _ignore,
        on_boot: Callable[[Sequence[str]], None] = _ignore,
    ) -> None:
        self._guests = list(guests)
        self._templates = {one.name: one for one in templates}
        self._machine = machine
        self._on_stop_error = on_stop_error
        self._segments = dict(segments or {})
        self._pcap = pcap
        self._on_segment = on_segment
        self._on_boot = on_boot
        self.up = Machines()
        """The guests that are up. One object for the whole run: a phase and
        the control socket's scope hold it, and never see a guest still booting."""
        self._all: Machines | None = None
        self._lans: list[Lan] = []
        self._requires: dict[str, list[str]] = {one.name: list(one.requires) for one in self._guests}
        # Set when a guest's boot has ended, either way.
        self._starting: dict[str, anyio.Event] = {}
        # Every guest whose boot began, so a failed one is torn down too.
        self._attempts: set[str] = set()
        # The first index an instance may take: the nodes' and the templates' own come before it.
        self._instance_base = len(self._guests) + len(self._templates)

    async def prepare(self) -> None:
        """Plug the segments, make each guest's `Machine` and pick its host
        ports. No guest boots."""
        if self._all is not None:
            raise RunError("already prepared")
        if self._pcap is not None:
            # A second run into the same directory would otherwise keep the
            # last run's captures beside its own.
            shutil.rmtree(self._pcap, ignore_errors=True)
        self._lans = build_lans(
            segment_members(self._guests),
            hubs={name for name, one in self._segments.items() if one.hub},
            pcap_dir=self._pcap,
            on_change=self._on_segment,
        )
        lan_fds: dict[str, dict[str, int]] = defaultdict(dict)
        for lan in self._lans:
            for member, fd in lan.fds.items():
                machine_name, nic_name = member.split("/")
                lan_fds[machine_name][nic_name] = fd
        self._all = Machines((one.name, self._machine(one, lan_fds.get(one.name))) for one in self._guests)

        for vm in self._all.values():
            vm.resolve_forward()
        routed = tuple(vm for vm in self._all.values() if vm.spec.uplink_address is not None)
        for vm in self._all.values():
            if vm.spec.router:
                vm.routed = routed
                for guest in routed:
                    guest.behind = vm

        for lan in self._lans:
            lan.start()
            if lan.name in self._segments and self._segments[lan.name].capture:
                await lan.capture()

    @property
    def machines(self) -> Mapping[str, Machine]:
        """Every guest's `Machine`, up or not."""
        return MappingProxyType(self._prepared())

    @property
    def lans(self) -> dict[str, Lan]:
        """The segments by name."""
        return {lan.name: lan for lan in self._lans}

    @property
    def templates(self) -> Mapping[str, MachineSpec]:
        return MappingProxyType(self._templates)

    @property
    def attempted(self) -> dict[str, Machine]:
        """Every guest whose boot began, a failed one included."""
        return {name: vm for name, vm in (self._all or {}).items() if name in self._attempts}

    async def ensure(self, names: Collection[str]) -> None:
        """Boot *names* and the guests they `require`, unless they are up.

        Two callers may ask for one guest at once (both need the cache), so
        a guest's boot is started once and the second caller waits for it.
        A guest that will not boot raises here, in the caller that needed it.
        """
        machines = self._prepared()
        wanted = sorted(required(names, self._requires))
        unknown = [name for name in wanted if name not in machines]
        if unknown:
            raise RunError(f"no such guest: {', '.join(unknown)}; the guests are {', '.join(machines)}")
        mine = [name for name in wanted if name not in self._starting]
        for name in mine:
            self._starting[name] = anyio.Event()
        if mine:
            self._on_boot(mine)
            self._attempts.update(mine)
            try:
                failures = await _start_all(machines[name] for name in mine)
            finally:
                for lan in self._lans:
                    lan.detach([f"{name}/" for name in mine])
            for name in mine:
                if name not in failures:
                    self.up[name] = machines[name]
                self._starting[name].set()
            if failures:
                raise next(iter(failures.values()))
        for name in wanted:
            await self._starting[name].wait()
        down = [name for name in wanted if name not in self.up]
        if down:
            raise MachineError(f"{', '.join(down)} did not boot")

    async def add(self, template: str, name: str) -> MachineSpec:
        """Make the guest *name* from *template*, plug it in and bind its
        forwards, as `prepare` does for a node; `ensure` boots it.

        It gets the lowest free index, so a terminated instance's MACs and
        addresses come back. nixidae/nixidae#1408.
        """
        machines = self._prepared()
        if template not in self._templates:
            raise InstanceError(f"no template {template!r}; the templates are {', '.join(self._templates) or 'none'}")
        if name in machines or name in self._templates:
            raise InstanceError(f"a guest or template is already called {name!r}")
        index = free_index({vm.spec.index for vm in machines.values()}, self._instance_base)
        addresses = {lan: segment.instance_addresses for lan, segment in self._segments.items()}
        one = instance_spec(self._templates[template], name, index, addresses, index - self._instance_base)
        lans = self.lans
        members = [(nic.name, nic.segment) for nic in one.interfaces]
        if one.uplink is not None:
            members.append(("vec0", one.uplink))
        lan_fds = {nic: lans[segment].add_member(f"{name}/{nic}") for nic, segment in members}
        vm = self._machine(one, lan_fds)
        try:
            vm.resolve_forward()
            if one.uplink_address is not None and (router := self._router()) is not None:
                await vm.route_through(router)
        except BaseException:
            # The error that stopped the add is the one to raise, not a
            # second one from an egress that refused it already.
            with anyio.CancelScope(shield=True), contextlib.suppress(MachineError):
                await vm.leave_router()
            vm.close_claims()
            self._unplug(name)
            raise
        machines[name] = vm
        self._requires[name] = list(one.requires)
        return one

    def _router(self) -> Machine | None:
        return next((vm for vm in self._prepared().values() if vm.spec.router), None)

    def _unplug(self, name: str) -> None:
        for lan in self._lans:
            for member in [m for m in lan.members if m.startswith(f"{name}/")]:
                lan.remove_member(member)

    def holds(self, name: str) -> bool:
        """Whether *name* is an instance the run still holds, booted or not."""
        vm = None if self._all is None else self._all.get(name)
        return vm is not None and vm.spec.index >= self._instance_base

    def instance(self, name: str) -> Machine:
        """The instance *name*; only a launched guest is one."""
        if not self.holds(name):
            raise InstanceError(f"{name!r} is no instance; only a launched guest is terminated")
        return self._prepared()[name]

    async def terminate(self, name: str) -> None:
        """Power off the instance *name* and unplug it; its index is free again."""
        vm = self.instance(name)
        # Shielded: a terminate that a cancel cuts short leaves a guest running and its ports plugged.
        with anyio.CancelScope(shield=True):
            try:
                await vm.leave_router()
            except MachineError as error:
                self._on_stop_error(name, error)
            await shut_down([vm], self._on_stop_error, kill=False)
        self._unplug(name)
        self.up.pop(name, None)
        del self._prepared()[name]
        self._attempts.discard(name)
        self._starting.pop(name, None)
        self._requires.pop(name, None)

    async def teardown(self, *, kill: bool = False) -> None:
        """Every guest down and every segment closed. Safe to call twice.

        *kill*: SIGKILL each guest, not ask it to power off; see
        `Machine.shutdown`.
        """
        await shut_down(self.attempted.values(), self._on_stop_error, kill=kill)
        # Emptied, not replaced: `up` is one object for the whole run.
        self.up.clear()
        self._all = None
        self._attempts = set()
        self._starting = {}
        for lan in self._lans:
            lan.close()
        self._lans = []

    def _prepared(self) -> Machines:
        if self._all is None:
            raise RunError("the run is not prepared, or is torn down")
        return self._all
