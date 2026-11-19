"""A run something else drives.

`run_test` did the whole sequence in one call and tore the guests down in
a `finally`, so there was no point at which anything outside could speak.
Everything the design wants needs that point: holding a failed run open,
stopping before a phase, running one phase later, an MCP server driving
the same object slowly.

So the session is the program and the CLI is one drive of it:

    session = Session(spec, out)
    async with session.live():
        await session.boot()
        for phase in session.pending():
            await session.run(phase)
        await session.teardown()

**Teardown is never automatic.** That is the whole point, and it is also
the risk: a caller that forgets leaves a UML kernel spinning on a core.
`vivarium_runner.lifetime.die_with_parent` covers the parent dying, which
covers the CLI. It does not cover an MCP server that stays up, so
whatever owns sessions has to reap them.

The mechanism underneath is `vivarium_runner`, unchanged. This module owns the
sequence, not the guests.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import math
import sys
import time
import traceback
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypedDict, cast

import anyio
import pytest
from anyio.from_thread import BlockingPortal
from vivarium_runner import Machine, MachineError, Machines, MachineSpec, backends
from vivarium_runner.backends import BackendError
from vivarium_runner.instances import seed
from vivarium_runner.report import Report
from vivarium_runner.run import Run, RunError, Segment, shut_down

from . import access, journal, junit_in
from .cloud import Cloud, CloudService
from .events import Event, Kind, Level
from .phases import (
    VERDICT,
    PhaseState,
    PhaseTimeout,
    Reloaded,
    ReloadRefused,
    Stop,
    Verdict,
    guests,
    passed,
    reloaded,
    runnable,
    skipped_by,
)
from .pytest_plugin import Plugin, arguments, machine_fixtures
from .roots import Roots
from .sinks import Broadcast

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Iterator, Mapping

    from anyio.abc import TaskGroup
    from vivarium_runner.machine_spec import Cpus, MachineSpecJson

    from .sinks import Sink
    from .spec import PhaseSpec, PytestSpec, Spec


class SessionError(RuntimeError):
    """The session could not do what was asked."""


_PRINTING: ContextVar[str | None] = ContextVar("uml_printing", default=None)
"""The phase whose task is printing. See `Session._capture`."""

_COPY: ContextVar[io.StringIO | None] = ContextVar("vivarium_copy", default=None)
"""Where a task's prints also go: the reply to an `exec` or `inject`."""


class _CaseFields(TypedDict, total=False):
    """The case a line belongs to, as an event field. Spread into `emit`."""

    case: str


def _output(emit: Callable[..., None], text: str, machine: str | None = None) -> None:
    """*text* as `OUTPUT` events of the phase that prints it, one per line,
    and into the `exec` or `inject` reply that runs it."""
    phase = _PRINTING.get()
    if (copy := _COPY.get()) is not None:
        copy.write(text)
    for line in text.splitlines():
        if line.strip():
            emit(Kind.OUTPUT, line, phase=phase, machine=machine)


class _Lines:
    """`sys.stdout` while phases run: each line an event of its phase."""

    def __init__(self, emit: Callable[..., None]) -> None:
        self.emit = emit

    def write(self, text: str) -> int:
        _output(self.emit, text)
        return len(text)

    def flush(self) -> None:
        pass


class CasesFailed(RuntimeError):
    """A pytest phase whose tests failed. Each one is its own event."""


def _pytest_main(args: list[str], plugins: list[object]) -> int:
    # The tests are in the store, which is read-only.
    sys.dont_write_bytecode = True
    return int(pytest.main(args, plugins=plugins))


def _forget(names: set[str], under: Path) -> None:
    """Drop the modules among `names` that were loaded from `under`."""
    root = under.resolve()
    for name in names:
        file = getattr(sys.modules.get(name), "__file__", None)
        if file is not None and Path(file).resolve().is_relative_to(root):
            del sys.modules[name]


def load_phase(script: Path) -> Callable[[Machines], Awaitable[None]]:
    """The `test` coroutine a phase's script exports.

    Imported, not `exec`'d. nixpkgs' driver runs `exec(tests, symbols)`
    with its methods injected as globals, and pays for it three ways: a
    script cannot import anything, nothing type checks it, and every
    frame is named `<string>` -- so the driver carries a traceback filter
    to make an assertion readable. A module has none of those problems.
    """
    spec = importlib.util.spec_from_file_location(f"uml_phase_{script.stem}", script)
    if spec is None or spec.loader is None:
        raise SessionError(f"cannot import a phase from {script}")
    module = importlib.util.module_from_spec(spec)
    # In sys.modules before it runs, as importlib's recipe for a source
    # file has it: dataclasses resolves a string annotation through the
    # module's entry there (nixidae/nixidae#1222).
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[spec.name]
        raise
    test: object = getattr(module, "test", None)
    if test is None:
        raise SessionError(f"{script} exports no `test`")
    if not callable(test):
        raise SessionError(f"`test` in {script} is not callable")
    # The script is user code, so nothing before this check says what `test`
    # is. The runner calls it with the machines and awaits the result.
    return cast("Callable[[Machines], Awaitable[None]]", test)


@dataclass(frozen=True, slots=True)
class PerGuest[T]:
    """A run's value for its guests: one guest's own, then `every` for each
    guest not named. `--cpus`, `--cpu-scale` and `--kernel`."""

    every: T | None = None
    guests: dict[str, T] = field(default_factory=dict)

    def of(self, guest: str) -> T | None:
        return self.guests.get(guest, self.every)

    def check(self, names: list[str], what: str) -> None:
        """Refuse a guest this names and the run lacks."""
        unknown = sorted(set(self.guests) - set(names))
        if unknown:
            raise SessionError(f"no guest {', '.join(unknown)} to give {what}; the guests: {', '.join(names)}")


def with_kernel(machines: list[MachineSpec], kernels: PerGuest[Path]) -> list[MachineSpec]:
    """The machines, booting the run's kernels instead of Nix's.

    Each guest's backend knows where its kernel is named, and refuses a
    guest that boots none of its own. Only ever reached from `--kernel`,
    by hand: the check keeps the kernel Nix built.
    """
    kernels.check([machine.name for machine in machines], "a kernel")
    out: list[MachineSpec] = []
    for machine in machines:
        kernel = kernels.of(machine.name)
        try:
            out.append(machine if kernel is None else backends.get(machine.backend).with_kernel(machine, kernel))
        except BackendError as error:
            raise SessionError(f"--kernel: {error}") from error
    return out


def with_cpus(machines: list[MachineSpecJson], override: PerGuest[Cpus]) -> list[MachineSpecJson]:
    """The machines, with the run's processors in place of `vivarium.cpus`.
    A guest the override names and the run lacks is refused."""
    override.check([m["name"] for m in machines], "processors")
    out: list[MachineSpecJson] = []
    for machine in machines:
        cpus = override.of(machine["name"])
        out.append(machine if cpus is None else {**machine, "cpus": cpus})
    return out


class Session:
    """One run, from evaluation to teardown, driven a step at a time."""

    def __init__(
        self,
        spec: Spec,
        out: Path,
        *,
        offline: bool = False,
        random_ports: bool = False,
        sink: Sink | None = None,
        pytest_args: list[str] | None = None,
        kernel: PerGuest[Path] | None = None,
        cpus: PerGuest[Cpus] | None = None,
    ) -> None:
        self.spec = spec
        # Kernels from a working tree, by hand. See `with_kernel`.
        self.kernel = kernel
        self.cpus = cpus
        self.out = out
        self.offline = offline
        self.random_ports = random_ports
        self.sink = sink
        # Added to every pytest phase's own `args`: `vivarium run ... -- -k x`.
        self.pytest_args = pytest_args or []
        # The pytest test running now, so a command or a journal entry
        # names the test that caused it and not only the phase.
        self.case: str | None = None
        self._started = time.monotonic()
        self.artifacts = out / "artifacts"
        self.state: dict[str, PhaseState] = {phase.name: PhaseState.PENDING for phase in spec.phases}
        self.errors: dict[str, str] = {}
        self.stopped: Stop | None = None
        """What ended the run before its verdict, if anything did."""
        # Which phase holds each guest, so a command or a journal entry
        # carries the phase it belonged to. By guest because two phases
        # on disjoint guests run at once, and a guest is only ever held by
        # one. "What did `cluster` spend its time on" is then a question
        # `events.jsonl` answers on its own.
        self.running: dict[str, str] = {}
        self._captures = 0
        self._stdout: Any = None
        self.vms: Machines | None = None
        # Per session: a process may hold several, and their timings are
        # not one run's.
        self.report = Report()
        # The guests, from `prepare` to `teardown`; `vms` is its guests that are up.
        self._run: Run | None = None
        self.cloud = Cloud(self)
        self.roots = Roots()
        self._tasks: TaskGroup | None = None
        self._journals: dict[str, journal.Tail] = {}
        # `follow` and a phase boundary both drain, and a Tail read twice
        # at once hands the same lines to both.
        self._draining = anyio.Lock()
        self._settled: set[str] = set()
        # A second run into the same `--out` finds the last run's files,
        # and their cases are not this run's. Not removed: a container
        # guest's files can belong to a sub-uid (nixidae/nixidae#1064).
        self._junit_seen: set[tuple[Path, int]] = set(junit_in.written(self.artifacts))
        # Process-wide, like `_capture`: one session per process, which
        # the CLI and the MCP server's child processes both are.
        for path in reversed(spec.pythonPath):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))

    @contextlib.asynccontextmanager
    async def live(self) -> AsyncIterator[None]:
        """Hold the run's background tasks: a cloud instance's boot, its notice.

        Boot, phases and teardown go inside it. A task still held at its
        end is cancelled, so a missed stop cannot keep the run up.
        """
        async with anyio.create_task_group() as group:
            self._tasks = group
            try:
                yield
            finally:
                self._tasks = None
                group.cancel_scope.cancel()

    @property
    def tasks(self) -> TaskGroup:
        """The task group `live` holds, for a task that outlives the call that starts it."""
        if self._tasks is None:
            raise SessionError("the session is not live: drive it inside `async with session.live()`")
        return self._tasks

    # ── events ─────────────────────────────────────────────────────

    def emit(
        self,
        kind: Kind,
        text: str,
        *,
        level: Level = Level.INFO,
        machine: str | None = None,
        phase: str | None = None,
        seconds: float | None = None,
        **data: object,
    ) -> None:
        """Say something, once, to every sink that wants it."""
        if self.sink is None:
            return
        self.sink.emit(
            Event(
                at=time.monotonic() - self._started,
                kind=kind,
                level=level,
                text=text,
                machine=machine,
                phase=phase,
                seconds=seconds,
                data=dict(data),
            )
        )

    def tap(self, sink: Sink) -> None:
        """Another sink beside the run's own: the control socket's feed."""
        self.sink = sink if self.sink is None else Broadcast([self.sink, sink])

    def _console(self, machine: str, line: str) -> None:
        self.emit(Kind.CONSOLE, line, level=Level.CONSOLE, machine=machine)

    def _say(self, machine: str, text: str) -> None:
        _output(self.emit, f"{text}\n", machine)

    def _command(self, machine: str, what: str, seconds: float, outcome: dict[str, Any]) -> None:
        self.emit(
            Kind.RPC,
            what,
            level=Level.DETAIL,
            machine=machine,
            seconds=seconds,
            phase=self.running.get(machine),
            **outcome,
            **self._in_case(),
        )

    @contextlib.contextmanager
    def _capture(self, phase: str | None = None, copy: io.StringIO | None = None) -> Iterator[None]:
        """Turn what a phase prints into events.

        A phase script says things with `print`, which is right -- asking
        a test author to learn a logging API to say "the cluster came up"
        is how a framework stops being used. But a bare print reaches the
        terminal and nothing else, so the log file and `events.jsonl`
        would be missing the one thing a reader most wants.

        Captured here instead, so each line becomes an OUTPUT event that
        carries the phase it came from. `Machine.say` takes the same path
        and adds the guest.

        **`sys.stdout` is process-wide**, and phases on disjoint guests
        run at once. So the first capture installs one writer and the
        last one out restores the real stdout, and the writer asks a
        context variable which phase is printing: each phase is its own
        task, and a task has its own context. `redirect_stdout` per phase
        would restore the terminal when the first of two phases ended,
        under the other one still printing.

        `copy` also receives the task's prints. Code sent through the
        control socket runs beside the phases, so its reply cannot take
        `sys.stdout` for itself either.
        """
        token = _PRINTING.set(phase)
        copying = _COPY.set(copy)
        if self._captures == 0:
            self._stdout = sys.stdout
            sys.stdout = _Lines(self.emit)
        self._captures += 1
        try:
            yield
        finally:
            self._captures -= 1
            if self._captures == 0:
                sys.stdout = self._stdout
            _COPY.reset(copying)
            _PRINTING.reset(token)

    def _replay(self, lines: int = 20, nodes: Collection[str] | None = None) -> None:
        """The end of each guest's console, at error level.

        This is what makes a quiet default safe. The console is off the
        terminal while things work, and the moment a phase fails the
        last lines of every guest arrive without anybody going to look
        for a file -- which is the context that usually explains it.

        nixpkgs has the switch (`print_serial_logs`) and not this: there
        the choice is all of it all the time, or none of it including
        when it would have helped.
        """
        for name, vm in self._attempted().items():
            if nodes is not None and name not in nodes:
                continue
            tail = [line for line in vm.console_tail(lines) if line]
            if not tail:
                continue
            self.emit(
                Kind.ERROR,
                f"the last {len(tail)} console lines from {name}:",
                level=Level.ERROR,
                machine=name,
            )
            for line in tail:
                self.emit(Kind.ERROR, f"  {line}", level=Level.ERROR, machine=name)

    # ── the operations ─────────────────────────────────────────────

    async def boot(self, only: Collection[str] | None = None) -> Machines:
        """Prepare the run, and bring every guest up, or only *only*.

        `only=()` boots nothing: each phase then boots its own guests
        when it starts (`ensure`).
        """
        await self.prepare()
        await self.ensure(self.every if only is None else only)
        return self.vms_up

    @property
    def every(self) -> frozenset[str]:
        """Every guest in the run, up or not."""
        names = {machine["name"] for machine in self.spec.machines}
        return frozenset(names | set(self.vms or ()))

    @property
    def vms_up(self) -> Machines:
        if self.vms is None:
            raise SessionError("run before boot")
        return self.vms

    def specs(self) -> tuple[list[MachineSpec], list[MachineSpec]]:
        """The guests and the templates, with the run's `--cpus` and `--kernel`.

        Raise `SessionError` for an override the run refuses. The CLI calls
        this before the run starts, so a refusal is one line, not a traceback.
        """
        machines = self.spec.machines
        templates = self.spec.templates
        # One list, so `--cpus NAME=N` finds a guest or a template.
        both = machines + templates
        if self.cpus is not None:
            both = with_cpus(both, self.cpus)
        specs = [MachineSpec.from_json(one) for one in both]
        if self.kernel is not None:
            specs = with_kernel(specs, self.kernel)
        return specs[: len(machines)], specs[len(machines) :]

    async def prepare(self) -> None:
        """What the whole run needs before any guest starts: the segments,
        each guest's forwards, the `Machine` of each guest. No guest boots."""
        if self._run is not None:
            raise SessionError("already prepared")
        specs, templates = self.specs()
        kernels = self.kernel or PerGuest[Path]()
        for one in specs:
            if (kernel := kernels.of(one.name)) is not None:
                self.emit(
                    Kind.NOTE,
                    f"{one.name} boots {kernel}, not the kernel Nix built; this is not the check",
                    level=Level.ERROR,
                    machine=one.name,
                    kernel=str(kernel),
                )
        for one in specs:
            if one.interfaces:
                self.emit(
                    Kind.BOOT,
                    f"{one.name}: "
                    + "; ".join(
                        f"{nic.name} on {nic.segment}" + (f" {' '.join(nic.addresses)}" if nic.addresses else "")
                        for nic in one.interfaces
                    ),
                    machine=one.name,
                )
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self._journals = {}
        run = Run(
            specs,
            templates,
            machine=self._machine,
            on_stop_error=self._stop_failed,
            segments={
                name: Segment(hub=one.hub, capture=one.capture, instance_addresses=tuple(one.instanceAddresses))
                for name, one in self.spec.segments.items()
            },
            pcap=self.out / "pcap",
            # The phase that cut or captured, so `jq` finds it by phase.
            on_segment=lambda text: self.emit(Kind.NOTE, text, phase=_PRINTING.get()),
            on_boot=lambda names: self.emit(Kind.BOOT, f"booting {', '.join(names)}"),
        )
        self._run = run
        await run.prepare()
        # Only the guests that are up. `exec` and a phase's `vms` never
        # see a guest that has not booted, which would hang them.
        vms = run.up
        vms.settings = self.spec.settings
        vms.artifacts = self.artifacts
        # A phase reads `vms.knobs["name"]` and gets the value, not the
        # record: a script branching on a steer should not have to know
        # where the steer came from.
        vms.knobs = {name: knob.value for name, knob in self.spec.knobs.items()}
        # One phase's findings for a later one -- a process census taken
        # before the suites, read by the phase that looks for leaks.
        vms.shared = {}
        vms.segments = run.lans
        vms.launcher = self
        vms.cloud = self.cloud
        vms.phase = None
        vms.pytest_args = self.pytest_args

        def guests() -> dict[str, access.Guest]:
            return {name: access.guest(vm.reachable(vm.spec.ssh_port), vm.forward) for name, vm in run.machines.items()}

        vms.access = access.write(guests(), self.out)
        for vm in run.machines.values():
            vm.on_inbound = lambda _vm: access.refresh(guests(), self.out)
        self.vms = vms

    def _machine(self, one: MachineSpec, lan_fds: dict[str, int] | None) -> Machine:
        """The `Machine` of one guest, with its journal tail: a node at `prepare`, an instance at `launch`."""
        path = self._guest_artifacts(one.name) / journal.FILE
        # A second run into the same `--out` would otherwise follow the
        # last run's journal as if this guest had written it.
        path.unlink(missing_ok=True)
        self._journals[one.name] = journal.Tail(path)
        vm = Machine(
            one,
            tasks=self.tasks,
            lan_fds=lan_fds,
            artifacts=self._guest_artifacts(one.name),
            recorder=self.report,
            offline=self.offline,
            random_ports=self.random_ports,
            on_console=self._console,
            on_command=self._command,
            on_say=self._say,
        )
        vm.service = CloudService(self.cloud, one.name)
        return vm

    async def launch(self, template: str, name: str, user_data: str | None = None) -> Machine:
        """Boot a new guest *name* from *template* while the run goes, and return it once up.

        It gets the lowest free index, so a terminated instance's MACs and
        addresses come back. nixidae/nixidae#1408.
        """
        if self._run is None or self.vms is None:
            raise SessionError("launch before prepare")
        one = self._run.add(template, name)
        (self._guest_artifacts(name) / "instance.json").write_text(json.dumps(seed(one, template, user_data)))
        self.emit(Kind.BOOT, f"launching {name} from {template}, index {one.index}", machine=name, template=template)
        await self.ensure([name])
        return self.vms[name]

    @property
    def templates(self) -> Mapping[str, MachineSpec]:
        """The run's templates by name, once prepared."""
        return MappingProxyType({}) if self._run is None else self._run.templates

    def holds(self, name: str) -> bool:
        """Whether *name* is a launched guest the session still holds, booted or not."""
        return self._run is not None and self._run.holds(name)

    async def terminate(self, name: str) -> None:
        """Power off an instance `launch` started and unplug it; its index is free again."""
        if self._run is None or self.vms is None:
            raise SessionError("terminate before prepare")
        self._run.instance(name)
        self.emit(Kind.BOOT, f"terminating {name}", machine=name)
        await self.drain()
        await self._run.terminate(name)
        self._journals.pop(name, None)

    async def ensure(self, names: Collection[str]) -> None:
        """Boot *names* and the guests they `require`, unless they are up.

        Two phases may ask for one guest at once (both need the cache), so
        a guest's boot is started once and the second caller waits for it.
        A guest that will not boot raises here, inside the phase that
        needed it, so that phase fails and its dependents are skipped.
        """
        vms = self.vms_up
        if self._run is None:
            # A session whose guests were handed in, as the unit tests do.
            missing = [name for name in names if name not in vms]
            if missing:
                raise SessionError(f"no guest {', '.join(missing)}, and nothing to boot it from")
            return
        try:
            # Captured: passt and the forward resolver say useful things
            # on the way up, and they say them with `print`.
            with self._capture():
                await self._run.ensure(names)
        except RunError as error:
            raise SessionError(str(error)) from error

    async def drain(self) -> None:
        """Emit every journal entry the guests have written since last asked.

        Attributed to the running phase. That is exact because both phase
        boundaries wait for the stream: a drain as the phase starts, and
        `settle` before it finishes.
        """
        async with self._draining:
            # A copy: an instance that ends leaves `_journals` while a read waits.
            for name, tail in list(self._journals.items()):
                for line in await tail.read():
                    entry = journal.parse(line)
                    if entry is None:
                        continue
                    if entry.identifier == journal.SETTLE:
                        self._settled.add(entry.message)
                        continue
                    self.emit(
                        Kind.JOURNAL,
                        entry.message,
                        level=journal.level(entry),
                        machine=name,
                        phase=self.running.get(name),
                        **entry.data(),
                        **self._in_case(),
                    )

    async def settle(self, timeout: float = 2.0, nodes: Collection[str] | None = None) -> None:
        """Wait until each guest's journal has reached the host.

        Only `nodes` when given: a phase settles its own guests, and a
        token sent to a guest another phase holds is a command in the
        middle of that phase.

        A test that logs and returns is done before journald has handed
        the line on, and a teardown straight after loses it: measured,
        `echo` from a unit that `systemd-run --wait` had finished never
        reached the file. So each live guest logs a token, and this
        drains until every token has arrived -- everything logged before
        it has arrived too.

        Bounded, and skipped for a guest that is dead or not streaming:
        evidence is worth two seconds, never a hung run.
        """
        if self.vms is None:
            return
        tokens: set[str] = set()

        async def send(vm: Machine) -> None:
            token = f"{vm.name}-{time.monotonic_ns()}"
            try:
                await vm.succeed(f"echo {token} | systemd-cat --identifier={journal.SETTLE}", timeout=timeout)
            except MachineError:
                return
            tokens.add(token)

        with self.report.waiting("-", "the journals to settle"), anyio.move_on_after(timeout):
            async with anyio.create_task_group() as group:
                for name, tail in self._journals.items():
                    if nodes is not None and name not in nodes:
                        continue
                    vm = self.vms.get(name)
                    if vm is not None and tail.streaming and vm.alive():
                        group.start_soon(send, vm)
            # Polled: the stream is a file a guest writes, and nothing says it grew.
            while not tokens <= self._settled:
                await self.drain()
                await anyio.sleep(0.02)
        await self.drain()

    def _cases_from_guests(self, phase: str, nodes: Collection[str] | None = None) -> None:
        """Every JUnit file a guest wrote during this phase, as cases.

        See `junit_in`. Keyed on the file and its mtime, so a suite that
        rewrites `unit.xml` in a later phase is read again, and a file
        read once is not read twice. Only `nodes` when given, so a file
        another phase is still writing is not read half-written.
        """
        for key in junit_in.written(self.artifacts):
            path = key[0]
            if nodes is not None and path.parent.parent.name not in nodes:
                continue
            if key in self._junit_seen:
                continue
            self._junit_seen.add(key)
            machine = path.parent.parent.name
            try:
                cases = junit_in.parse(path.read_text(errors="replace"))
            except ValueError as error:
                self.emit(
                    Kind.ERROR,
                    f"{path.name} from {machine}: {error}",
                    level=Level.ERROR,
                    machine=machine,
                    phase=phase,
                )
                continue
            for case in cases:
                bad = case.outcome in ("failed", "error")
                fields = {
                    key: value
                    for key, value in (
                        ("message", case.message),
                        ("error", case.error),
                        ("reason", case.reason),
                    )
                    if value is not None
                }
                self.emit(
                    Kind.CASE,
                    case.name,
                    level=Level.ERROR if bad else Level.DETAIL,
                    machine=machine,
                    phase=phase,
                    seconds=case.seconds,
                    outcome=case.outcome,
                    when="guest",
                    file=path.name,
                    **fields,
                )

    def _in_case(self) -> _CaseFields:
        return {"case": self.case} if self.case is not None else {}

    async def follow(self, interval: float = 0.25) -> None:
        """Stream the guests' journals into the events, until cancelled.

        Run beside the phases by whatever drives the session -- `drive`
        in a task group, an MCP server in a task of its own. The session
        holds no task of its own, so it has no lifetime to get wrong.
        """
        try:
            while True:
                await self.drain()
                await anyio.sleep(interval)
        finally:
            # What the guests wrote on the way down.
            with anyio.CancelScope(shield=True):
                await self.drain()

    async def begin_case(self, nodeid: str) -> None:
        """A pytest test starts. What happens until `end_case` is its.

        Exact for a command. A journal entry is attributed when it
        arrives, and one logged in a test's last milliseconds can arrive
        after the test ends: a barrier per test would cost a command per
        guest per test. The phase has one, see `settle`.
        """
        await self.drain()
        self.case = nodeid

    async def end_case(self) -> None:
        await self.drain()
        self.case = None

    async def _pytest(
        self,
        name: str,
        spec: PytestSpec,
        vms: Machines,
        *,
        by_hand: bool = False,
        deadline: float | None = None,
        timeout: float | None = None,
        on_case: Callable[[str], None] | None = None,
    ) -> str:
        """One pytest run, in a worker thread, against these guests.

        `by_hand` is a run sent to a paused session, not a phase: it takes
        only its own arguments, and its cases stay out of junit.xml.
        `on_case` gets one line per case as it ends, on the loop.
        """
        # `--import-mode=importlib` puts nothing on `sys.path`, so a test
        # could not import a helper module beside it without this.
        here = spec.tests if spec.tests.is_dir() else spec.tests.parent
        sys.path.insert(0, str(here))
        imported = set(sys.modules)
        try:
            async with BlockingPortal() as portal:
                plugin = Plugin(
                    self, name, portal, by_hand=by_hand, deadline=deadline, timeout=timeout, on_case=on_case
                )
                plugins = [plugin, machine_fixtures(vms)]
                extra = spec.args if by_hand else [*spec.args, *self.pytest_args]
                args = arguments(str(spec.tests), extra)
                code = await anyio.to_thread.run_sync(_pytest_main, args, plugins)
        finally:
            sys.path.remove(str(here))
            # pytest's importlib mode hands back a module already in
            # `sys.modules`, and so does a plain import of a helper. So
            # a second run of an edited file would be the first run's code.
            _forget(set(sys.modules) - imported, here)
        if code == pytest.ExitCode.NO_TESTS_COLLECTED:
            # A phase that tested nothing is a selection that matched
            # nothing, and a green run would hide the typo.
            raise CasesFailed("no tests were collected")
        if code == pytest.ExitCode.USAGE_ERROR:
            # pytest says why on its own stderr, not through any hook.
            raise CasesFailed(f"pytest rejected its arguments: {args[1:]}")
        if plugin.timed_out is not None:
            raise PhaseTimeout(f"{plugin.timed_out}; {plugin.summary()}")
        if code != pytest.ExitCode.OK:
            raise CasesFailed(plugin.summary() if plugin.outcomes else f"pytest exited {code}")
        self.emit(Kind.NOTE, plugin.summary(), phase=name)
        return plugin.summary()

    def view(self, phase: PhaseSpec, nodes: Collection[str] | None = None) -> Machines:
        """The guests `phase` declared, as the `vms` its script is given.

        Its own object, because two phases run at once and each has its
        own `phase`. Everything else is shared by reference: `shared` is
        how one phase leaves a finding for the next. A phase that reaches
        a guest it did not declare fails on the name, which is the whole
        check that `nodes` is true.
        """
        if self.vms is None:
            raise SessionError("run before boot")
        wanted = list(nodes) if nodes is not None else phase.nodes or list(self.vms)
        missing = [name for name in wanted if name not in self.vms]
        if missing:
            # A node whose `networking.hostName` is not its attribute name
            # boots under the hostname.
            raise SessionError(
                f"phase {phase.name} names {', '.join(missing)} in `nodes`; the guests are {', '.join(self.vms)}"
            )
        vms = Machines((name, self.vms[name]) for name in wanted)
        vms.settings = self.vms.settings
        vms.artifacts = self.vms.artifacts
        vms.access = self.vms.access
        vms.knobs = self.vms.knobs
        vms.shared = self.vms.shared
        vms.segments = self.vms.segments
        vms.launcher = self.vms.launcher
        vms.cloud = self.vms.cloud
        vms.pytest_args = self.pytest_args
        # One script may serve several phases, told apart by this.
        vms.phase = phase.name
        return vms

    async def run(self, phase: PhaseSpec) -> PhaseState:
        """Run one phase, and record what its outcome means for the rest.

        Reentrant for phases on disjoint guests: everything it touches
        per phase is keyed by the phase or by its guests. Which phases
        may overlap is `phases.launchable`'s answer, not this method's.
        """
        if self.vms is None:
            raise SessionError("run before boot")
        nodes = self.needs(phase)
        await self.drain()
        self.state[phase.name] = PhaseState.RUNNING
        for name in nodes:
            self.running[name] = phase.name
        self.emit(Kind.PHASE_STARTED, phase.name, phase=phase.name, nodes=nodes)
        started = time.monotonic()
        deadline = None if phase.timeout is None else anyio.current_time() + phase.timeout
        try:
            await self.ensure(nodes)
            vms = self.view(phase, nodes)
            if phase.pytest is not None:
                await self._pytest(phase.name, phase.pytest, vms, deadline=deadline, timeout=phase.timeout)
            elif phase.script is not None:
                # Loaded inside the `try`: a script that does not import
                # is this phase failing. Outside it, an ImportError took
                # the whole drive down with no failed phase and no pause.
                test = load_phase(phase.script)
                limit = anyio.CancelScope(deadline=math.inf if deadline is None else deadline)
                with self._capture(phase.name), limit:
                    await test(vms)
                if limit.cancelled_caught:
                    raise PhaseTimeout(f"phase {phase.name} ran past its timeout of {phase.timeout:g}s")
        except Exception as error:  # noqa: BLE001 -- any error fails the phase, and the run goes on
            took = time.monotonic() - started
            await self.settle(nodes=nodes)
            self._cases_from_guests(phase.name, nodes)
            self._record(phase, started)
            self.state[phase.name] = PhaseState.FAILED
            self.errors[phase.name] = f"{type(error).__name__}: {error}"
            self.emit(
                Kind.PHASE_FINISHED,
                f"{phase.name} FAILED: {error}",
                level=Level.ERROR,
                phase=phase.name,
                seconds=took,
                state=str(PhaseState.FAILED),
                error=self.errors[phase.name],
            )
            # A failing test has already said why, with pytest's own
            # rewritten assertion. The runner's traceback would only add
            # the frames of the runner.
            if not isinstance(error, CasesFailed):
                self.emit(
                    Kind.ERROR,
                    traceback.format_exc().rstrip(),
                    level=Level.ERROR,
                    phase=phase.name,
                )
            self._replay(nodes=nodes)
            for name in skipped_by(phase.name, self.spec.phases):
                if self.state.get(name) is PhaseState.PENDING:
                    self.state[name] = PhaseState.SKIPPED
                    self.emit(
                        Kind.PHASE_FINISHED,
                        f"{name} skipped, it needs {phase.name}",
                        phase=name,
                        state=str(PhaseState.SKIPPED),
                        reason=f"{phase.name} failed",
                    )
            self._release(nodes)
            return PhaseState.FAILED
        except BaseException:
            # Cancelled: the run is being stopped. Recorded, then passed
            # on, because a stop must not be swallowed here.
            self.state[phase.name] = PhaseState.INTERRUPTED
            self.emit(
                Kind.PHASE_FINISHED,
                f"{phase.name} interrupted",
                level=Level.ERROR,
                phase=phase.name,
                seconds=time.monotonic() - started,
                state=str(PhaseState.INTERRUPTED),
                error="the run was stopped while this phase ran",
            )
            self._release(nodes)
            raise
        took = time.monotonic() - started
        await self.settle(nodes=nodes)
        self._cases_from_guests(phase.name, nodes)
        self._release(nodes)
        self._record(phase, started)
        self.state[phase.name] = PhaseState.PASSED
        self.emit(
            Kind.PHASE_FINISHED,
            f"{phase.name} passed in {took:.1f}s",
            phase=phase.name,
            seconds=took,
            state=str(PhaseState.PASSED),
        )
        return PhaseState.PASSED

    def needs(self, phase: PhaseSpec) -> list[str]:
        """The guests *phase* runs on, which must be up before it starts."""
        if phase.nodesFromDependents:
            return sorted(guests(phase, self.spec.phases, self.state, self.every))
        return list(phase.nodes) or sorted(self.every)

    def _release(self, nodes: list[str]) -> None:
        for name in nodes:
            self.running.pop(name, None)

    def _record(self, phase: PhaseSpec, started: float) -> None:
        """A phase is a span in the timings as well as a row in the state.

        The same unit everywhere -- report, event stream, breakpoint name,
        MCP tool -- rather than three names for one thing.
        """
        self.report.step("-", "phase", phase.name, time.monotonic() - started)

    def write_output(self) -> None:
        """Everything a reader needs, in the directory the caller named.

        The same four things whether or not this ran in a sandbox, which
        is the point: one writer for a run by hand and for the check,
        because two writers drift, and each difference is a bug somebody
        meets later.

            status       0 or 1, as text
            report.json  where the time went
            phases.json  what each phase did, and why it was skipped
            artifacts/   what the guests wrote to /artifacts
        """
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "status").write_text("0\n" if self.passed else "1\n")
        stopped = None if self.stopped is None else self.stopped.reason
        (self.out / VERDICT).write_text(Verdict.of(self.state, self.errors, stopped).to_json())
        self.write_report()
        verdict = "passed" if self.passed else "failed"
        if self.stopped is not None:
            verdict = f"stopped ({self.stopped.reason})"
        self.emit(
            Kind.RUN_FINISHED,
            verdict,
            level=Level.ERROR if not self.passed else Level.INFO,
            passed=self.passed,
            stopped=None if self.stopped is None else self.stopped.reason,
            states={name: str(state) for name, state in self.state.items()},
        )

    def write_report(self) -> None:
        """`report.json`. Written with the verdict, and again after the
        teardown so that it holds the teardown's steps: the verdict does
        not wait for the guests to go down."""
        first = next(iter(self.errors.values()), None)
        self.report.write(self.out / "report.json", self.passed, first)

    def reload(self, spec: Spec) -> Reloaded:
        """Take the phases, settings and knobs of `spec`; the guests stay.

        Refused when the guests differ: a guest is booted from its spec,
        and a changed one needs a new run, or `vivarium switch`."""
        if spec.machines != self.spec.machines:
            raise ReloadRefused("the guests changed; a reload takes phases only: start a new run, or switch a guest")
        if spec.segments != self.spec.segments:
            raise ReloadRefused("the segments changed; a segment is wired at boot: start a new run")
        result = reloaded(self.spec.phases, spec.phases, self.state)
        self.spec = spec
        self.state = result.state
        if self.vms is not None:
            self.vms.settings = spec.settings
            self.vms.knobs = {name: knob.value for name, knob in spec.knobs.items()}
        return result

    def pending(self) -> list[PhaseSpec]:
        """The phases still worth running, in the order Nix sorted them."""
        return runnable(self.spec.phases, self.state)

    @property
    def passed(self) -> bool:
        """A stopped run has no verdict, so it never passes."""
        return self.stopped is None and passed(self.state)

    def _stop_failed(self, name: str, error: Exception) -> None:
        self.emit(Kind.ERROR, f"{name} did not shut down cleanly: {error}", level=Level.ERROR, machine=name)

    async def teardown(self, *, kill: bool = False) -> None:
        """Guests down. Safe to call twice; never called for you.

        *kill*: SIGKILL each guest, not ask it to power off; see
        `Machine.shutdown`.
        """
        await self.cloud.close()
        if self._run is None:
            await shut_down(self._attempted().values(), self._stop_failed, kill=kill)
        else:
            await self._run.teardown(kill=kill)
            self._run = None
        self.vms = None

    def _attempted(self) -> dict[str, Machine]:
        """Every guest whose boot began, a failed one included."""
        return dict(self.vms or {}) if self._run is None else self._run.attempted

    # ── helpers ────────────────────────────────────────────────────

    def _guest_artifacts(self, name: str) -> Path:
        """One guest's own directory, made before it boots.

        Per guest, because three nodes writing `pytest.log` into one
        directory is two lost files. Made here and not in the guest:
        hostfs and virtiofs both serve a directory that exists.

        Mode 1777, as /tmp: the runner is root of its own namespace, so
        without it only a guest's root could write here.
        """
        path = self.artifacts / name
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o1777)
        return path
