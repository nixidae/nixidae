"""The run's guests, as a test sees them: `vms`."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, Protocol

import anyio

from .machine import Machine, MachineError
from .net import Lan


class Launcher(Protocol):
    """Boots and powers off guests made from templates; the session is one."""

    async def launch(self, template: str, name: str, user_data: str | None = None) -> Machine: ...
    async def terminate(self, name: str) -> None: ...


@dataclass(frozen=True, slots=True)
class Issuer:
    """A CA a lab signs its servers' certificates with, as PEM text."""

    certificate: str
    key: str
    root: str
    """The certificate the host trusts, which signed `certificate`."""


class Trust(Protocol):
    """The run's lab CA: `vivarium.trust.Trust`."""

    async def issuer(self) -> Issuer:
        """The run's intermediate CA, made on first call. Its root goes to
        `access/ca.pem`, and with the host's public roots to
        `access/ca-bundle.pem`."""
        ...


class Unneeded(Exception):  # noqa: N818 -- a phase's outcome, not an error
    """`Machines.skip`: the phase found its work already done."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Exports(Protocol):
    """Environment variables a run hands the host's shell: `vivarium.exports.Exports`."""

    async def set(self, name: str, value: str) -> None:
        """Add or replace *name* in `access/environment.json`, which
        `vivarium env` and `vivarium shell` read."""
        ...


class CloudControl(Protocol):
    """The provider's side of the run's cloud API, for a phase: `vivarium.cloud.Cloud`. nixidae/nixidae#1408."""

    capacity: dict[str, int]
    """Live instances a template may have; one past it answers 409."""
    boot_delay: float
    """Seconds a launch stays `pending` before its guest boots."""

    async def wait_running(self, instance_id: str, timeout: float = 300) -> Machine: ...
    async def wait_ended(self, instance_id: str, timeout: float = 300) -> None: ...
    async def terminate(self, instance_id: str) -> None: ...
    async def interrupt(self, instance_id: str, seconds: float) -> None: ...


@dataclass(frozen=True, slots=True)
class RunContext:
    """What one run gives every phase beside its guests. The session makes
    it once; each phase's `Machines` holds the same object, and a reload
    replaces it."""

    artifacts: Path
    """Where this run's evidence goes.  Each guest sees its own
    subdirectory as ``/artifacts``, so a test collects a file by writing
    it in the guest and nothing is copied afterwards."""

    access: Path
    """Where a phase puts what the host needs to reach the guests: a
    kubeconfig, a token. ``<out>/access/``, emptied as the run starts;
    ``<out>/access.json`` and ``<out>/ssh_config`` sit beside it."""

    # Any: a test's own settings, read by the test that set them.
    settings: dict[str, Any] = field(default_factory=dict)
    """Whatever the spec's ``settings`` held -- values a test needs that
    only Nix knows, such as a package version or an image tag.  Empty
    unless ``mkTest`` was given some."""

    knobs: dict[str, str] = field(default_factory=dict)
    """What this run was told from outside, by name.

    Declared in Nix and resolved there, so a knob can change what is
    *built* as well as what a phase does. Every declared name is present;
    one whose variable is unset carries its declared default, which is
    what a sandboxed check always gets."""

    # Any: each phase defines its own findings, and the key says what type.
    shared: dict[str, Any] = field(default_factory=dict)
    """One phase's findings for a later phase of the same run.

    The phases are separate modules, so a value one computes -- a
    process census taken before the suites -- reaches the phase that
    compares against it only through here."""

    # Any: each phase defines its own keys; they need only be JSON.
    state: dict[str, Any] = field(default_factory=dict)
    """What a named environment's phases keep across its runs, beside its
    disks: a phase's own keys. Empty in every other run. A phase reads it
    to decide whether its work is done, and ends with `skip` if it is."""

    previous: Mapping[str, str] = field(default_factory=dict)
    """Each phase's state (`passed`, `unneeded`, `failed` ...) in the
    environment's last run that reached a verdict. Empty in every other run."""

    pytest_args: list[str] = field(default_factory=list)
    """What followed ``--`` on the run's command line. Every pytest phase
    gets them; a script that runs pytest inside a guest passes them on."""

    segments: Mapping[str, Lan] = field(default_factory=dict)
    """The run's Ethernet segments by name."""

    cloud: CloudControl | None = None
    """The run's cloud, which guests with `vivarium.cloud` call."""

    launcher: Launcher | None = None
    """What boots a guest from a template while the run goes: the session."""

    trust: Trust | None = None
    """The run's lab CA."""

    exports: Exports | None = None
    """What `export` writes to, one object for the whole run."""


class Machines(dict[str, Machine]):
    """The run's machines by name, also reachable as attributes.

    Parameterised because a bare ``dict`` makes ``values()`` and
    ``items()`` Unknown, and a caller's pyright then checks nothing.

    The run's values (`settings`, `artifacts`, ...) are read-only
    properties of `context`, so every phase's mapping reads one object.
    They are properties and not attributes: an unset attribute falls
    through to ``__getattr__`` and reads as a guest that does not exist.
    """

    def __init__(
        self,
        guests: Mapping[str, Machine] | Iterable[tuple[str, Machine]] = (),
        /,
        *,
        context: RunContext | None = None,
        phase: str | None = None,
    ) -> None:
        super().__init__(guests)
        self.context = context
        self.phase = phase
        """The phase running now. One script can serve several phases, told
        apart by name: ``mkTest`` phases generated from one list in Nix.
        ``None`` between phases."""

    def view(self, names: Iterable[str], phase: str) -> Machines:
        """The guests *names*, as *phase* sees them, with this run's context."""
        return Machines(((name, self[name]) for name in names), context=self.context, phase=phase)

    def _run(self) -> RunContext:
        if self.context is None:
            raise MachineError("these guests belong to no run: nothing gave them its settings, artifacts or access")
        return self.context

    @property
    def settings(self) -> dict[str, Any]:  # Any: see `RunContext.settings`
        return self._run().settings

    @property
    def artifacts(self) -> Path:
        return self._run().artifacts

    @property
    def access(self) -> Path:
        return self._run().access

    @property
    def knobs(self) -> dict[str, str]:
        return self._run().knobs

    @property
    def shared(self) -> dict[str, Any]:  # Any: see `RunContext.shared`
        return self._run().shared

    @property
    def state(self) -> dict[str, Any]:  # Any: see `RunContext.state`
        return self._run().state

    @property
    def previous(self) -> Mapping[str, str]:
        return self._run().previous

    @property
    def pytest_args(self) -> list[str]:
        return self._run().pytest_args

    @property
    def segments(self) -> Mapping[str, Lan]:
        return {} if self.context is None else self.context.segments

    @property
    def cloud(self) -> CloudControl | None:
        return None if self.context is None else self.context.cloud

    @property
    def launcher(self) -> Launcher | None:
        return None if self.context is None else self.context.launcher

    @property
    def trust(self) -> Trust | None:
        return None if self.context is None else self.context.trust

    @property
    def exports(self) -> Exports | None:
        return None if self.context is None else self.context.exports

    async def export(self, name: str, value: str) -> None:
        """Give `vivarium shell` the variable *name*: a path in `access`,
        an address of a forward. A forward's address is the one at export
        time; a guest whose forwards change exports again."""
        if self.exports is None:
            raise MachineError("this run exports nothing: no session drives it that can")
        await self.exports.set(name, value)

    def skip(self, reason: str) -> NoReturn:
        """End this phase as `unneeded`: its work is done already, as a
        kept environment's state shows. Phases after it run, and the run
        can still pass. A pytest phase skips a case with `pytest.skip`."""
        raise Unneeded(reason)

    async def launch(self, template: str, name: str, user_data: str | None = None) -> Machine:
        """Boot a new guest *name* from `templates.<template>`; it is in this mapping once up. nixidae/nixidae#1408."""
        if self.launcher is None:
            raise MachineError("this run launches no guests: nothing drives it that can")
        # A phase holds a copy of the run's mapping; the session adds to its own.
        vm = self[name] = await self.launcher.launch(template, name, user_data)
        return vm

    async def terminate(self, name: str) -> None:
        """Power off a guest `launch` started and unplug it from its segments."""
        if self.launcher is None:
            raise MachineError("this run launches no guests: nothing drives it that can")
        await self.launcher.terminate(name)
        self.pop(name, None)

    def segment(self, name: str) -> Lan:
        """One segment, to ``cut``, ``restore`` or ``capture``."""
        try:
            return self.segments[name]
        except KeyError:
            raise MachineError(f"no segment {name!r} in this test; have {', '.join(self.segments) or 'none'}") from None

    async def until[T](
        self,
        what: str,
        check: Callable[[Machine], Awaitable[T | None]],
        timeout: float = 120,
        poll: float = 1.0,
        machines: Iterable[str] | None = None,
        evidence: Callable[[Machine], Awaitable[str]] | None = None,
    ) -> dict[str, T]:
        """`Machine.until` on every guest at once, or on *machines*; each guest's result by name.

        A guest that comes good stops polling. One error names every
        guest that did not.
        """
        results: dict[str, T] = {}
        failed: dict[str, MachineError] = {}

        async def one(vm: Machine) -> None:
            async def asked() -> T | None:
                return await check(vm)

            async def seen() -> str:
                return await evidence(vm) if evidence is not None else ""

            try:
                results[vm.name] = await vm.until(what, asked, timeout, poll, seen if evidence is not None else None)
            except MachineError as error:
                failed[vm.name] = error

        async with anyio.create_task_group() as tg:
            for name in self if machines is None else machines:
                tg.start_soon(one, self[name])
        if failed:
            raise MachineError(
                f"{what}: failed on {', '.join(sorted(failed))}\n"
                + "\n".join(str(failed[name]) for name in sorted(failed))
            )
        return results

    def __getattr__(self, name: str) -> Machine:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(f"no machine {name!r} in this test; have {', '.join(self)}") from None
