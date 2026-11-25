"""A single NixOS guest, whatever kind of machine it turns out to be.

The runner gives every backend the same fds:

    the uplink    the egress, or a segment -- outbound NAT plus the host's way in
    one per NIC   from :mod:`vivarium_runner.net` -- L2 between guests
    a socketpair  arpyc to the guest's agent, on a serial line

Commands go over the socketpair, so nothing here depends on guest
networking having come up, and everything works inside a Nix build
sandbox.  :mod:`vivarium_runner.backends` turns those fds into an argv; this
file does not know which kind of machine it started.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

import anyio
from anyio.abc import TaskGroup, UNIXSocketStream
from vivarium_agent.arpyc import AsyncConnection, ProtocolError, Service, connect
from vivarium_agent.channel import AGENT_READY, AgentRpc, ProcessInfo, SystemState, UnitInfo

from . import backends, display, forward, ocr, report, storeview
from .backends.host import EGRESS_ANSWER, egress_exchange, memory_bytes
from .boot import Boot, BootspecJson
from .capability import ControlError, Controls, Framebuffer, Power, Screen
from .consolelines import ConsoleLines, GuestExited
from .control import (
    AGENT,
    NONE,
    CommandResult,
    ConsoleConfig,
    ConsoleControl,
    ConsoleError,
    Control,
    Dialect,
    NoneConfig,
    dialect as find_dialect,
    validate as validate_control,
)
from .guestprocess import GuestProcess
from .held import Held, hold
from .machine_spec import Interface, MachineSpec, Systems
from .store import Store, StoreError

POWEROFF_GRACE = 20
"""Seconds a guest has to power itself off before the runner signals it."""

ROLLBACK_WITHIN = 2 * EGRESS_ANSWER
"""Seconds a failed switch's forwards have to close again."""

RESETS_BEFORE_UP = 3
"""Resets a guest may make before it comes up, before the runner gives up.
OVMFFull resets once on its first boot through the firmware (measured on
`boot-counting` with `vivarium.secureBoot`), and a key enrollment resets
once more."""


BOOTLOADER = "bootloader.json"
"""The bootspec of the system the guest boots next, under its artifacts.
The guest's `installBootLoader` writes it; see modules/guest.nix."""

_SYSTEMD_TIMEOUT = 60
"""How long to wait for one `systemctl` round trip.  The agent gives the
command itself 30s, so anything past this is the guest, not systemd."""


class MachineError(Exception):
    """A guest failed to boot, or a command in it did not do as told."""


type SwitchAction = Literal["switch", "boot", "test", "dry-activate"]
"""What `switch-to-configuration` does with the new system."""


class GuestTimeout(MachineError):
    """A command or agent call ran past its own timeout in the guest. The
    guest killed it, and still answers."""


OUTPUT_LIMIT = 2000
"""How much of a command's output its event keeps: the end, where the
error usually is. The whole output still reaches the caller."""


def _agent_rpc(conn: AsyncConnection) -> AgentRpc:
    # rpyc reaches the peer's methods by name, so it carries no types.
    return cast(AgentRpc, conn.root)


@dataclass(frozen=True)
class AgentControl:
    """`Control` through the vivarium agent: a shell, with its exit status."""

    conn: AsyncConnection

    async def execute(self, command: str, timeout: float, env: Mapping[str, str] | None = None) -> CommandResult:
        # The wire carries a dict; another Mapping would not pickle.
        rc, out, err = await _agent_rpc(self.conn).run(command, timeout=timeout, env=None if env is None else dict(env))
        return CommandResult(rc, out, err)

    async def aclose(self) -> None:
        self.conn.close()


def _both(result: CommandResult) -> str:
    """stdout, then stderr: the whole of what a command said, for a person."""
    return "\n".join(filter(None, [result.stdout, result.stderr]))


def _command_outcome(result: CommandResult) -> dict[str, object]:
    return {"exit": result.returncode, "output": _both(result)[-OUTPUT_LIMIT:]}


class Machine:
    """Boots a guest and drives it, in the style of a NixOS test node."""

    artifacts: Path | None
    """A host directory this guest sees at ``/artifacts``.  What the guest
    writes there is on the host the moment it is written, so it survives a
    guest that never answers again."""

    def __init__(
        self,
        spec: MachineSpec,
        *,
        tasks: TaskGroup,
        lan_fds: dict[str, int] | None = None,
        artifacts: Path | None = None,
        kept: Path | None = None,
        boot_timeout: float = 180,
        # Generous, because several guests on a loaded builder are slow
        # in a way that looks exactly like a hang.
        command_timeout: float = 120,
        recorder: report.Report | None = None,
        offline: bool = False,
        random_ports: bool = False,
        on_console: Callable[[str, str], None] | None = None,
        on_command: Callable[[str, str, float, dict[str, object]], None] | None = None,
        on_say: Callable[[str, str], None] | None = None,
    ) -> None:
        self.spec = spec
        # The owner's, for the whole run: the guest's background tasks start
        # in one call (a boot) and end in another (a shutdown).
        self._tasks = tasks
        self.backend = backends.get(spec.backend)
        # Here, not at start: a run makes every Machine before it boots
        # any, so one guest's bad launch config stops the run before
        # another guest boots.
        self.backend.validate(spec)
        validate_control(spec.name, spec.control)
        self.lan_fds = lan_fds or {}
        """Each interface's segment fd, by interface name."""
        self.artifacts = artifacts
        self.kept = kept
        """Where `vivarium.persist` lives on the host: a named environment's
        `disks/<guest>`. None is a store in the run directory, gone with
        the run."""
        # A session passes its run's, so every guest's timings are in one
        # report. Without one, the guest keeps its own.
        self.recorder = recorder if recorder is not None else report.Report()
        self.boot_timeout = boot_timeout
        self.command_timeout = command_timeout
        # A run-time choice, not a property of the built guest: the same
        # image runs either way. A sandboxed run is offline whatever this
        # says, because the sandbox has no network for the egress to use.
        self.offline = offline
        # Every forward through an egress on any free host port: a build's,
        # where nobody outside reaches one (nixidae/nixidae#1546).
        self.random_ports = random_ports
        self.on_console = on_console
        self.on_command = on_command
        self.on_say = on_say
        self.declared: list[forward.Rule] = list(spec.inbound)
        """`vivarium.inbound` as asked, then each runtime add: what a switch
        diffs against. `forward` holds the same entries as bound."""
        self.forward: list[forward.Rule] = list(spec.inbound)
        self.on_inbound: Callable[[Machine], None] | None = None
        """Told after a runtime add or remove: the session writes `access.json` again."""
        self.behind: Machine | None = None
        """The router whose egress serves this guest, if one does."""
        self.egress_relay: str | None = None
        """Where the egress this guest's boot started reaches the guests' relay."""
        self._egress_lock = anyio.Lock()
        self.claims: list[forward.Claim] = []
        """The host sockets of this guest's forwards through an egress, bound
        before it boots and kept until it is gone: a reboot's egress gets them again."""
        self.egress_control: socket.socket | None = None
        """The control socket of the egress this guest's boot started; closing it ends the egress."""
        self.service: Service | None = None
        """What answers this guest's own calls to the host: the session's cloud API. Set before boot."""
        self.routed: tuple[Machine, ...] = ()
        """The router's guests, whose forwards its egress binds. The session
        sets it once every guest's forwards are resolved."""

        self._rundir: Path | None = None
        self._process: GuestProcess | None = None
        # Pumped from `_boot` to `_stop_process`, kept across both.
        self._console = ConsoleLines(self._log)
        self._agent_sock: socket.socket | None = None
        self._guest_sock: socket.socket | None = None
        self._conn: AsyncConnection | None = None
        self._spare_fds: list[int] = []
        self._controls = Controls()
        self._disk_fds: tuple[int, ...] = ()
        self._lan_kept: dict[str, int] = {}
        self._watcher: Held | None = None
        self._stopping = False
        self._exit_code: int | None = None
        self._exited = anyio.Event()
        """Set when the guest stopped by itself: a poweroff, a halt or a crash, never a host shutdown or a reboot."""
        self._reboot_error: str | None = None
        self._resets = 0
        """Resets since the guest last came up: see `RESETS_BEFORE_UP`."""
        self._control: Control | None = None
        self._transcript: anyio.AsyncFile[bytes] | None = None
        self._store: Store | None = None
        """What the guest sees of the host's store, when its spec names `storePaths`."""

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def systems(self) -> Systems:
        """The system this guest booted and the ones `switch_to` takes by name."""
        return self.spec.systems

    @property
    def control_kind(self) -> str:
        """How the host drives this guest: `agent`, `none`, or a console
        dialect's name. A console guest's `execute` runs its own command
        language."""
        return self.spec.control.kind

    @property
    def ip(self) -> str | None:
        return self.spec.ip

    @property
    def uplink_ip(self) -> str | None:
        """`vec0`'s IPv4 address as vivarium gives it: the router's lease
        for this guest, else the relay's fixed one; the router's own is
        its bridge address. `None` on a segment a peer serves, which picks
        its own."""
        if self.spec.uplink_address is not None:
            return self.spec.uplink_address
        if self.spec.uplink is None:
            return self.spec.uplink_network.address
        return None

    def __repr__(self) -> str:
        return f"<Machine {self.name}>"

    # ── lifecycle ──────────────────────────────────────────────────

    def resolve_forward(self) -> None:
        """Bind this guest's forwards, before anything is spawned.

        A forward that cannot be set up is a `MachineError` naming the
        guest: the run reports it as a failed boot, not as a traceback.
        """
        if self.forward and not self.spec.forwards_through_egress:
            raise MachineError(
                f"[{self.name}] forward: no egress serves this guest's uplink, so nothing binds its forwards"
            )
        try:
            self.forward, self.claims = forward.claim(forward.resolve(self.forward), random=self.random_ports)
        except forward.ForwardError as error:
            raise MachineError(f"[{self.name}] forward: {error}") from error
        for rule in self.forward:
            self._log(f"forwarding {', '.join(str(port) for port in rule.ports)} on {rule.address}")

    def _runtime_inbound(self) -> Machine:
        """The machine whose egress serves this guest's forwards; a
        `MachineError` for a guest no egress serves."""
        if not self.spec.forwards_through_egress:
            raise MachineError(f"[{self.name}] inbound: no egress serves this guest's uplink")
        if self.behind is None and self.spec.uplink_address is not None and not self.spec.router:
            # Its own egress does not exist: a forward told to it is lost.
            raise MachineError(f"[{self.name}] inbound: this guest sits behind a router the run has not given it")
        return self.behind or self

    async def route_through(self, router: Machine) -> None:
        """Serve this guest's forwards from *router*'s egress, which runs
        already when an instance is launched behind it."""
        self.behind = router
        router.routed = router.routed + (self,)  # noqa: RUF005 -- zuban types the unpacked form tuple[object, ...]
        relay, address = router.egress_relay, self.uplink_ip
        if relay is not None and address is not None:
            await self._tell_egress(router, list(forward.egress_adds(self.claims, address, relay)))

    async def leave_router(self) -> None:
        """Take this guest's forwards off its router's egress: the guest goes away."""
        router, self.behind = self.behind, None
        if router is None:
            return
        router.routed = tuple(vm for vm in router.routed if vm is not self)
        await self._tell_egress(router, [forward.Remove(claim) for claim in self.claims])

    async def _tell_egress(self, owner: Machine, messages: list[forward.Add | forward.Remove]) -> None:
        """*messages* to *owner*'s egress. None while it is down: the next
        boot hands over `claims`, which holds the change already."""
        async with owner._egress_lock:  # noqa: SLF001 -- one lock per egress, held by its owner
            if owner.egress_control is None:
                return
            try:
                await anyio.to_thread.run_sync(egress_exchange, owner.egress_control, messages)
            except backends.BackendError as error:
                raise MachineError(f"[{self.name}] inbound: {error}") from error

    async def open_inbound(
        self,
        guest: int,
        host: int | None = None,
        protocols: tuple[str, ...] = ("tcp",),
        address: str | None = None,
    ) -> list[str]:
        """Forward *guest* from the host while the guest runs, as a
        `vivarium.inbound` entry would: *host* or any free port, on
        *address* or `forward.LOOPBACK`. Where it answers, as `reachable`."""
        owner = self._runtime_inbound()
        if any(
            port.guest == guest and set(protocols) & set(rule.protocols) for rule in self.forward for port in rule.ports
        ):
            raise MachineError(f"[{self.name}] inbound: guest port {guest} is forwarded already")
        asked = forward.Rule(address=address, ports=(forward.PortMap(host, guest),), protocols=protocols)
        try:
            [rule] = forward.resolve([asked])
            [bound], claims = forward.claim([rule], random=self.random_ports)
        except forward.ForwardError as error:
            raise MachineError(f"[{self.name}] inbound: {error}") from error
        relay, guest_ip = owner.egress_relay, self.uplink_ip
        try:
            if owner.egress_control is not None and (relay is None or guest_ip is None):
                raise MachineError(f"[{self.name}] inbound: the egress runs, but this guest has no uplink address")
            adds = forward.egress_adds(claims, guest_ip, relay) if relay and guest_ip else []
            await self._tell_egress(owner, list(adds))
        except BaseException:
            for claim in claims:
                claim.sock.close()
            raise
        self.declared.append(asked)
        self.forward.append(bound)
        self.claims += claims
        self._inbound_changed()
        return forward.reachable([bound], guest, protocols[0])

    async def close_inbound(self, guest: int, protocol: str | None = None) -> None:
        """Stop forwarding *guest* (*protocol* only, if given): its host
        port closes and its open flows end."""
        owner = self._runtime_inbound()
        gone = [
            index
            for index, rule in enumerate(self.forward)
            if any(port.guest == guest for port in rule.ports) and (protocol is None or protocol in rule.protocols)
        ]
        if not gone:
            raise MachineError(f"[{self.name}] inbound: guest port {guest} is not forwarded")
        await self._close(owner, gone, protocol)

    async def _close(self, owner: Machine, gone: list[int], protocol: str | None) -> None:
        """Close the entries at *gone* in `forward` (*protocol* only, if given)."""
        ends = {
            (rule.address, port.host, port.guest)
            for index in gone
            for rule in [self.forward[index]]
            for port in rule.ports
        }
        closing = [
            claim
            for claim in self.claims
            if (*claim.sock.getsockname()[:2], claim.guest) in ends and (protocol is None or claim.protocol == protocol)
        ]
        await self._tell_egress(owner, [forward.Remove(claim) for claim in closing])
        for claim in closing:
            claim.sock.close()
        self.claims = [claim for claim in self.claims if claim not in closing]

        def narrowed(rule: forward.Rule) -> forward.Rule | None:
            left = tuple(one for one in rule.protocols if protocol is not None and one != protocol)
            return replace(rule, protocols=left) if left else None

        declared: list[forward.Rule] = []
        bound: list[forward.Rule] = []
        for index, (asked, rule) in enumerate(zip(self.declared, self.forward, strict=True)):
            if index not in gone:
                declared.append(asked)
                bound.append(rule)
            elif (still := narrowed(rule)) is not None:
                declared.append(replace(asked, protocols=still.protocols))
                bound.append(still)
        self.declared, self.forward = declared, bound
        self._inbound_changed()

    async def switch_inbound[T](self, wanted: list[forward.Rule], switch: Callable[[], Awaitable[T]]) -> T:
        """Run *switch*, and change this guest's forwards to *wanted*, its
        new `vivarium.inbound`: by difference, so a port that stays keeps its
        host port and its flows.

        A new guest port binds before *switch*, so a taken port stops the
        switch with nothing changed; a failed *switch* closes them again.
        Removals follow it, then the entries whose host port, address or
        protocols changed, which need their old socket gone first.
        """
        gone, added = forward.diff(self.declared, wanted)
        if not gone and not added:
            return await switch()
        owner = self._runtime_inbound()
        staying = {port.guest for rule in gone for port in rule.ports}
        fresh = [rule for rule in added if not {port.guest for port in rule.ports} & staying]
        later = [rule for rule in added if rule not in fresh]
        opened: list[forward.Rule] = []
        try:
            for rule in fresh:
                [port] = rule.ports
                await self.open_inbound(port.guest, port.host, rule.protocols, rule.address)
                opened.append(rule)
            done = await switch()
        except BaseException:
            with anyio.CancelScope(shield=True) as scope:
                scope.deadline = anyio.current_time() + ROLLBACK_WITHIN
                await self._close(owner, [self.declared.index(rule) for rule in opened], None)
            raise
        await self._close(owner, [self.declared.index(rule) for rule in gone], None)
        for rule in later:
            [port] = rule.ports
            await self.open_inbound(port.guest, port.host, rule.protocols, rule.address)
        return done

    def _inbound_changed(self) -> None:
        for rule in self.forward:
            self._log(f"forwarding {', '.join(str(port) for port in rule.ports)} on {rule.address}")
        if self.on_inbound is not None:
            self.on_inbound(self)

    async def start(self) -> None:
        """Boot the guest and connect to its agent."""
        if self._process is not None:
            return

        self._rundir = Path(tempfile.mkdtemp(prefix=f"guest-{self.name}-"))
        if self.spec.store_paths is not None:
            # The two closures share most of their paths, and a view takes each once.
            paths = list(dict.fromkeys([*storeview.read_paths(self.spec.store_paths), *self._late_paths()]))
            self._store = self.backend.store(self._rundir, paths)
            self.spec = self._store.serve(self.spec)
        # The session closes its copies of the segment fds once every guest
        # has started, so a guest that boots again needs its own.
        if self.backend.reboots:
            self._lan_kept = {nic: os.dup(fd) for nic, fd in self.lan_fds.items()}
        await self._boot(None)
        if self.spec.late_closure is not None:
            await self._load_registration(str(self.spec.late_closure))

    def _late_paths(self) -> list[str]:
        info = self.spec.late_closure
        return [] if info is None else [*storeview.read_paths(info / "store-paths"), str(info)]

    async def _load_registration(self, info: str) -> None:
        """Register a closureInfo in the guest's Nix database, if it has one."""
        drop = "echo 2 > /proc/sys/vm/drop_caches && " if self.backend.has_kernel else ""
        nix_store = self.spec.nix_store
        load = "true" if nix_store is None else f"{shlex.quote(nix_store)} --load-db < {shlex.quote(info)}/registration"
        done = await self.execute(f"{drop}{load}", label=f"add_closure {Path(info).name}")
        if done.returncode != 0:
            raise MachineError(f"[{self.name}] loading {info} failed (exit {done.returncode}):\n{_both(done)}")

    async def _boot(self, boot: Boot | None) -> None:
        """Start the guest's process, on *boot* or the spec's own, and connect."""
        if self._rundir is None:
            raise MachineError(f"[{self.name}] boot before start: no run directory")
        self._agent_sock, self._guest_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

        # The boot clock starts before the launch: a launch waits on its
        # helpers, and a clock started at the spawn hid that.
        started = time.monotonic()
        try:
            if boot is None:
                launch = await self.backend.launch(self, self._rundir, self._guest_sock.fileno(), self.lan_fds)
            else:
                launch = await self.backend.launch(
                    self,
                    self._rundir,
                    self._guest_sock.fileno(),
                    self.lan_fds,
                    boot=boot,
                    disk_fds=self._disk_fds,
                )
        except backends.BackendError as error:
            # A host that lacks something is a reason, not a crash: said
            # the way a guest that did not boot is said.
            raise MachineError(f"[{self.name}] {error}") from None
        for fd in set(self._disk_fds) - set(launch.disk_fds):
            os.close(fd)
        self._disk_fds = launch.disk_fds
        self._controls = launch.controls
        # Fds the backend opened for the child and no longer needs here.
        self._spare_fds = [
            fd
            for fd in launch.pass_fds
            if fd != self._guest_sock.fileno() and fd not in self.lan_fds.values() and fd not in self._disk_fds
        ]
        for note in launch.notes:
            self.say(note)
        self._log(f"exec: {' '.join(launch.argv)}")

        process = self._process = await GuestProcess.spawn(launch)
        for fd in self._spare_fds:
            os.close(fd)
        self._spare_fds = []
        self.recorder.step(self.name, "launch", f"launch {self.name}", time.monotonic() - started)
        if process.stdout is None:
            raise MachineError(f"[{self.name}] no guest process console to read")
        self._console.start(self._tasks, process.stdout, f"console of {self.name}")
        if launch.controls.power is not None:
            await self._run_cpus(launch.controls.power)

        if self.control_kind not in (AGENT, NONE):
            await self._start_console(find_dialect(self.name, self.spec.control), started)
            self._watch_from_here()
            return

        none = NoneConfig.of(self.name, self.spec.control) if self.control_kind == NONE else None
        if none:
            await self._read_serial()
        ready = none.ready if none else re.compile(re.escape(AGENT_READY))
        try:
            with anyio.fail_after(self.boot_timeout):
                # This boot's process: a reset before the guest is up is
                # `_boot`'s to handle, and the next process is not started.
                await self._wait_for_line(ready, process.wait)
            elapsed = time.monotonic() - started
            self._log(f"up in {elapsed:.1f}s")
            self.recorder.booted(
                self.name,
                elapsed,
                {
                    "backend": self.spec.backend,
                    "cpus": self.spec.cpus,
                    "memory": self.spec.memory,
                },
            )
        except TimeoutError:
            raise MachineError(
                f"[{self.name}] "
                + (f"console never matched {none.ready.pattern!r}" if none else "agent did not come up")
                + f" within {self.boot_timeout:g}s"
            ) from None
        except MachineError:
            if not await self._rebooted():
                raise
            # The firmware or the bootloader reset the guest before it was
            # up, as it does on a machine: it boots again.
            self._resets += 1
            if self._resets > RESETS_BEFORE_UP:
                raise MachineError(f"[{self.name}] reset {self._resets} times before it came up") from None
            for sock in (self._agent_sock, self._guest_sock):
                if sock is not None:
                    sock.close()
            self._agent_sock = self._guest_sock = None
            await self._reboot()
            return
        self._resets = 0

        if none:
            # Nothing in the guest to connect to: tests reach it over the network.
            self._watch_from_here()
            return

        if launch.agent_path is not None:
            # No serial line to carry the socketpair: the agent listens on
            # a socket the host bound in, and says so before it accepts.
            self._agent_sock.close()
            self._agent_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._agent_sock.connect(str(launch.agent_path))
        self._conn = connect(self._agent_sock.detach(), self.service)
        # It ends when the connection closes: a shutdown, a reboot, the guest gone.
        await self._tasks.start(self._conn.serve_forever)
        self._control = AgentControl(self._conn)
        self._agent_sock = None
        # Drop our copy of the guest's end, so the connection reports EOF
        # when the guest goes away rather than hanging on our own fd.
        self._guest_sock.close()
        self._guest_sock = None
        self._watch_from_here()

    def _watch_from_here(self) -> None:
        """Hand the guest's exit to `_watch`, now that the guest is up."""
        self._watcher = hold(self._tasks, self._watch, name=f"watch {self.name}")

    async def _rebooted(self) -> bool:
        """Did the guest's process end because the guest asked for a reboot?"""
        if self._process is None or self._process.returncode is None:
            return False
        power = self._controls.power
        cause = None if power is None else await power.shutdown_cause()
        return self.backend.rebooted(backends.Stop(self._process.returncode, cause))

    async def _run_cpus(self, power: Power) -> None:
        """Start the CPUs of a guest that its backend started stopped."""
        try:
            with anyio.fail_after(self.boot_timeout):
                await power.run()
        except (OSError, EOFError, ControlError, TimeoutError) as error:
            raise MachineError(f"[{self.name}] the monitor did not start the guest: {error or 'timed out'}") from None

    async def _watch(self) -> None:
        """Boot the guest again when it reboots itself; record a stop it made by itself.

        A reset ends the process: QEMU runs with `-no-reboot`, and libkrun
        has no other way to stop. Rebooting in place would load the kernel
        and `init=` it started with, not the system the guest switched its
        bootloader to. The backend says whether the exit was a reboot.
        """
        if self._process is None:
            raise MachineError(f"[{self.name}] no guest process to wait for")
        await self._process.wait()
        await self._console.drained()
        if self._stopping:
            return
        if await self._rebooted():
            try:
                await self._reboot()
            except MachineError as error:
                self._reboot_error = str(error)
                self._log(f"did not come back from its reboot: {error}")
            return
        self._gone(self._process.returncode or 0)

    def _gone(self, code: int) -> None:
        """The guest's process ended with no host shutdown: no call may reach its agent again."""
        self._log(f"stopped by itself, exit {code}")
        # A later call would write to a closed fd, or one the next guest reuses.
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._control = None
        self._exit_code = code
        self._exited.set()

    async def exited(self) -> int:
        """Wait until the guest stops by itself, and return its process's exit code.

        A poweroff exits 0. A crash, a panic or a kill is another code. A
        reboot does not count, and a host `shutdown` never sets it: the
        caller that shuts a guest down already knows.
        """
        await self._exited.wait()
        if self._exit_code is None:
            raise MachineError(f"[{self.name}] stopped with no exit code")
        return self._exit_code

    def _bootloader(self) -> BootspecJson | None:
        """What the guest's bootloader chose since it booted, if it wrote a choice."""
        path = self.artifacts / BOOTLOADER if self.artifacts is not None else None
        if path is None or not path.exists():
            return None
        # The bootloader writes this file, so its shape is Nix's, not the parser's.
        return cast(BootspecJson, json.loads(path.read_text())["org.nixos.bootspec.v1"])

    async def _reboot(self) -> None:
        """The process-scoped half of `shutdown`, then `_boot` on the same disk."""
        if self.spec.boot is None:
            raise MachineError(f"[{self.name}] rebooted, and its spec has no boot to boot again")
        boot = self.spec.boot.rebooted(self._bootloader())
        if boot.system is not None:
            self._log(f"rebooting into {boot.system}")
        else:
            self._log("rebooting through its firmware" if boot.firmware is not None else "rebooting")
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        await self._close_console()
        # The next boot's agent is a new one: until it is up, a command
        # reaches no guest, and says why.
        self._control = None
        await self._stop_process()
        # A second boot must not match the first boot's ready line.
        self._console.reset()
        self._process = None
        self.lan_fds = dict(self._lan_kept)
        await self._boot(boot)

    async def _read_serial(self) -> None:
        """Read the serial line as console, for a guest with no control channel.

        The serial line is the socket the agent would get.
        """
        if self._agent_sock is None or self._guest_sock is None:
            raise MachineError(f"[{self.name}] no serial socket")
        sock, self._agent_sock = self._agent_sock, None
        self._guest_sock.close()
        self._guest_sock = None
        await self._console.read_serial(self._tasks, sock, f"serial of {self.name}")

    async def _start_console(self, dialect: Dialect, started: float) -> None:
        """Log in on the guest's console and apply its config disk.

        The serial port carries the socketpair the agent would get. Every
        byte read goes to `console.raw` among the guest's artifacts.
        *started* is when the launch began, which the boot time counts from.
        """
        if self._agent_sock is None or self._guest_sock is None:
            raise MachineError(f"[{self.name}] no console socket")
        sock, self._agent_sock = self._agent_sock, None
        self._guest_sock.close()
        self._guest_sock = None
        if self.artifacts is not None:
            await anyio.Path(self.artifacts).mkdir(parents=True, exist_ok=True)
            self._transcript = await anyio.open_file(self.artifacts / "console.raw", "ab")
        console = ConsoleControl(
            await UNIXSocketStream.from_socket(sock),
            dialect,
            transcript=self._transcript.write if self._transcript is not None else None,
        )
        self._control = console
        try:
            await console.login(timeout=self.boot_timeout)
        except (TimeoutError, ConsoleError) as error:
            raise MachineError(
                f"[{self.name}] no {dialect.name} prompt within {self.boot_timeout:g}s"
                f" ({error or 'timed out'}); the console is in console.raw"
            ) from None
        self._log(f"logged in on the {dialect.name} console")
        if dialect.configure is not None and ConsoleConfig.of(self.name, self.spec.control).configure:
            done = await self.execute(dialect.configure, timeout=self.boot_timeout, label="apply the config disk")
            if done.returncode != 0:
                raise MachineError(f"[{self.name}] the config disk failed:\n{_both(done)}")
        elapsed = time.monotonic() - started
        self._log(f"up in {elapsed:.1f}s")
        self.recorder.booted(
            self.name,
            elapsed,
            {"backend": self.spec.backend, "cpus": self.spec.cpus, "memory": self.spec.memory},
        )

    async def _close_console(self) -> None:
        """The console login of a console guest, and its transcript."""
        if isinstance(self._control, ConsoleControl):
            with contextlib.suppress(OSError, anyio.BrokenResourceError, anyio.ClosedResourceError):
                await self._control.aclose()
            self._control = None
        if self._transcript is not None:
            await self._transcript.aclose()
            self._transcript = None

    async def shutdown(self, *, kill: bool = False) -> None:
        """Ask the guest to power off, then make sure nothing is left.

        *kill* SIGKILLs the guest and does not ask. For a guest whose disk
        is thrown away and whose evidence is read already: an orderly
        poweroff costs seconds, and a guest that does not finish it in
        `POWEROFF_GRACE` costs all of them (measured, 20 s of the 81 s of
        `checks.k8s`).
        """
        self._stopping = True
        if self._watcher is not None:
            await self._watcher.stop()
            self._watcher = None
        # A console guest keeps nothing, so it is not asked: QEMU is
        # stopped below.
        await self._close_console()
        # `alive` first: a dead guest's agent does not refuse, it hangs,
        # and the request below waited out its full timeout after `crash`.
        asked = False
        if not kill and self._conn is not None and not self._conn.closed and self.alive():
            # Asked even when the reply is lost: poweroff can take the
            # agent down before it answers.
            asked = True
            try:
                with self.recorder.span(self.name, "teardown", f"poweroff {self.name}"):
                    await self._ask("poweroff", self._agent.poweroff(), 15)
            except (MachineError, OSError, EOFError):
                pass
            except Exception as error:  # noqa: BLE001 -- the agent refused; the guest is reaped below all the same
                self._log(f"the poweroff request failed: {type(error).__name__}: {error}")
            self._conn.close()
            self._conn = None
        self._control = None

        for sock in (self._agent_sock, self._guest_sock):
            if sock is not None:
                sock.close()
        self._agent_sock = self._guest_sock = None

        with self.recorder.span(self.name, "teardown", f"reap {self.name}"):
            await self._reap(POWEROFF_GRACE if asked else 0, kill=kill)
        with self.recorder.span(self.name, "teardown", f"clean up {self.name}"):
            await self._stop_process()
            self._close_kept()
            if self._store is not None:
                self._store.remove()
                self._store = None
            if self._rundir is not None:
                if not os.environ.get("VIVARIUM_KEEP"):
                    shutil.rmtree(self._rundir, ignore_errors=True)
                self._rundir = None

    def _close_kept(self) -> None:
        """The disk and segment fds and the claimed ports kept for a reboot."""
        for fd in (*self._disk_fds, *self._lan_kept.values()):
            os.close(fd)
        self._disk_fds = ()
        self._lan_kept = {}
        self.close_claims()

    def close_claims(self) -> None:
        """Close the host sockets of this guest's forwards."""
        for held in self.claims:
            held.sock.close()
        self.claims = []

    async def _stop_process(self) -> None:
        """What one boot of the guest leaves after its process exited."""
        await self._console.stop()
        if self.egress_control is not None:
            self.egress_control.close()
            self.egress_control = None

        channel, self._controls = self._controls.channel, Controls()
        if channel is not None:
            # The guest is already gone by here in the ordinary case, and a
            # monitor that cannot be said goodbye to must not stop the rest
            # of the teardown.
            with contextlib.suppress(OSError, EOFError, ControlError):
                await channel.close()

        if self._process is not None:
            self._process.release()

    async def _reap(self, grace: float, *, kill: bool = False) -> None:
        """Wait *grace* seconds for the guest to exit, then SIGTERM its
        process group, then SIGKILL it. *kill* goes to SIGKILL at once."""
        if self._process is not None and not await self._process.reap(grace, kill=kill):
            self._log("process would not die")

    def alive(self) -> bool:
        """Is the guest's process still running? Asks the host, not the
        agent: a dead guest's agent does not answer, it hangs."""
        return self._process is not None and self._process.alive()

    async def crash(self) -> None:
        """Kill the guest outright, the way a power cut would.

        SIGKILL: no shutdown, nothing flushed, no chance to say goodbye.
        For a test of what survives a dead node. `shutdown` afterwards is
        still safe and still needed.
        """
        if self._process is None:
            raise MachineError(f"[{self.name}] not started")
        pid = self._process.guest_pid()
        self._process.signal(signal.SIGKILL)
        await self._process.wait()
        self._close_kept()
        # Polled: under UML the kernel is not our child, so there is
        # nothing to wait on.
        for _ in range(50):
            if not self.alive():
                return
            await anyio.sleep(0.1)
        raise MachineError(f"[{self.name}] pid {pid} survived SIGKILL")

    async def wait(self, timeout: float | None = 90) -> int:
        """Wait for the UML process to exit; returns its exit code."""
        if self._process is None:
            raise MachineError(f"[{self.name}] not started")
        try:
            with anyio.fail_after(timeout):
                await self._process.wait()
        except TimeoutError:
            self._process.signal(signal.SIGTERM)
            return -1
        return self._process.returncode or 0

    # ── console ────────────────────────────────────────────────────

    def _log(self, message: str) -> None:
        """One console line, to `on_console`: a file per guest, a filter, an event queue."""
        if self.on_console is not None:
            self.on_console(self.name, message)

    def say(self, text: str) -> None:
        """Progress about this guest that a reader wants at the default
        verbosity, written as a phase would print it: a recipe's steps.

        `on_say` takes it; unset, it joins the console stream.
        """
        if self.on_say is not None:
            self.on_say(self.name, text)
        else:
            self._log(text)

    async def _wait_for_line(self, pattern: re.Pattern[str], gone: Callable[[], Awaitable[object]]) -> str:
        """Match *pattern* against console output, past or future, until *gone* returns."""
        try:
            return await self._console.wait_for(pattern, gone)
        except GuestExited:
            raise MachineError(f"[{self.name}] guest exited while waiting for {pattern.pattern!r}") from None

    async def wait_for_console_text(self, pattern: str, timeout: float | None = None) -> str:
        """Wait for a regex to appear on the guest's console.

        The wait goes on across a reboot, and ends when the guest stops by
        itself (`exited`).
        """
        with anyio.fail_after(timeout or self.command_timeout):
            return await self._wait_for_line(re.compile(pattern), self._exited.wait)

    # ── commands ───────────────────────────────────────────────────

    @property
    def _agent(self) -> AgentRpc:
        if self.control_kind == NONE:
            raise MachineError(f"[{self.name}] has no control channel: reach it over the network")
        if self.control_kind != AGENT:
            raise MachineError(
                f"[{self.name}] is driven through its {self.control_kind} console, and has no agent for this call"
            )
        if self._conn is None:
            raise self._unreachable("agent is not connected")
        return _agent_rpc(self._conn)

    def _unreachable(self, why: str) -> MachineError:
        """The error for a call that cannot reach the guest: why the guest is
        gone, when a reboot it did not come back from says so."""
        if self._reboot_error is not None:
            return MachineError(f"[{self.name}] did not come back from its reboot: {self._reboot_error}")
        return MachineError(f"[{self.name}] {why}")

    async def _ask[T](
        self,
        what: str,
        call: Awaitable[T],
        timeout: float,
        outcome: Callable[[T], dict[str, object]] | None = None,
    ) -> T:
        """Await one agent call, turning silence into a real error.

        Every call goes through here.  A guest that has wedged or run out
        of memory simply stops replying, and without a deadline the test
        would sit on the future until the whole run is killed -- with no
        clue as to which machine, or what it was asked.

        Every call is also timed here, for the same reason: this is the
        one place all of them pass through.  See report.py.

        *outcome* turns the reply into what the command's event carries,
        so a reader of `events.jsonl` sees how a command ended, not only
        that it ran.

        A `TimeoutError` that *call* raises is the guest's own timeout,
        not silence: it becomes `GuestTimeout`.
        """
        started = time.monotonic()
        data: dict[str, object] = {}
        deadline: anyio.CancelScope | None = None
        try:
            with anyio.fail_after(timeout) as deadline:
                result = await call
            if outcome is not None:
                data = outcome(result)
            return result
        except TimeoutError as error:
            if deadline is None or not deadline.cancelled_caught:
                # `args`, not `str`: rpyc rebuilds an OSError as
                # "[Errno None] None: None -> None" plus the remote traceback.
                said = str(error.args[0]) if error.args else "timed out"
                data = {"error": f"timed out in the guest: {said}"[:OUTPUT_LIMIT]}
                raise GuestTimeout(f"[{self.name}] {what}: {said}") from None
            data = {"error": "the guest stopped answering"}
            raise MachineError(
                f"[{self.name}] guest stopped answering during: {what}\n{self._console_tail()}"
            ) from None
        except EOFError:
            data = {"error": "the guest went away"}
            raise MachineError(f"[{self.name}] guest went away during: {what}\n{self._console_tail()}") from None
        except ProtocolError as error:
            data = {"error": f"the agent channel broke: {error}"}
            raise MachineError(f"[{self.name}] the agent channel broke during: {what}: {error}") from None
        except Exception as error:
            data = {"error": f"{type(error).__name__}: {error}"[:OUTPUT_LIMIT]}
            raise
        finally:
            took = time.monotonic() - started
            self.recorder.step(self.name, "rpc", what, took)
            if self.on_command is not None:
                # The same choke point the timings use, so a new kind of
                # call is reported without touching this.
                self.on_command(self.name, what, took, data)

    def console_tail(self, lines: int = 15) -> list[str]:
        """The guest's last *lines* console lines, oldest first."""
        return self._console.tail(lines)

    def _console_tail(self, lines: int = 15) -> str:
        """The last thing the guest said, for an error that has no other
        evidence to offer -- a wedged guest cannot be asked anything."""
        return "\n".join(f"    | {line}" for line in self.console_tail(lines)) or "    | (silent)"

    async def execute(
        self,
        command: str,
        timeout: float | None = None,
        label: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run a shell command in the guest; returns how it ended.

        *label* is what the timing report calls this step.  A command
        carrying shell plumbing -- a redirect kept so that a deadline has
        something to read -- is unreadable as a report line and says
        nothing the program name does not.

        *env* is added to this one command's environment, and is how a
        declared impurity reaches the guest -- ``vms.env`` on the host,
        chosen by the script, and never the whole host environment.
        """
        timeout = timeout or self.command_timeout
        if self._control is None:
            raise self._unreachable("not connected")
        # The guest kills the command at `timeout`; give the round trip
        # longer, so its error is what we report, not ours.
        return await self._ask(
            label or command,
            self._execute(self._control, command, timeout, env),
            timeout + 10,
            outcome=_command_outcome,
        )

    async def _execute(
        self, control: Control, command: str, timeout: float, env: Mapping[str, str] | None
    ) -> CommandResult:
        try:
            return await control.execute(command, timeout, env)
        except ConsoleError as error:
            raise MachineError(f"[{self.name}] {error}") from None

    async def succeed(
        self,
        command: str,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run a command that must succeed; returns how it ended."""
        done = await self.execute(command, timeout=timeout, env=env)
        if done.returncode != 0:
            raise MachineError(f"[{self.name}] command failed (exit {done.returncode}): {command}\n{_both(done)}")
        return done

    async def fail(
        self,
        command: str,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run a command that must fail; returns how it ended."""
        done = await self.execute(command, timeout=timeout, env=env)
        if done.returncode == 0:
            raise MachineError(f"[{self.name}] command unexpectedly succeeded: {command}\n{_both(done)}")
        return done

    async def switch_to(
        self,
        name: str | None = None,
        action: SwitchAction = "switch",
        *,
        check: bool = True,
        timeout: float = 300,
    ) -> CommandResult:
        """Switch to `vivarium.configurations.<name>`, to a system's store
        path, or back to the booted system with no *name*, the way
        `nixos-rebuild` does. A store path the host built after boot needs
        `add_closure` first.

        `switch` and `boot` point the system profile at it first. Then its
        `switch-to-configuration` runs in a transient unit, as nixos-rebuild
        runs it: the switch may restart the unit that started it, and here
        that is the agent. Exit 4 means a unit failed; *check* raises on any
        nonzero exit."""
        if self.control_kind != AGENT:
            raise MachineError(f"[{self.name}] runs {self.control_kind}, and has no NixOS to switch")
        if self.systems.toplevel is None:
            raise MachineError(
                f"[{self.name}] is a {self.spec.kind} guest (vivarium.kinds.{self.spec.kind}), not NixOS, "
                "and has no NixOS to switch"
            )
        if name is None:
            if self.systems.toplevel is None:
                raise MachineError(f"[{self.name}] the spec names no booted system")
            system = self.systems.toplevel
        elif name.startswith("/nix/store/"):
            system = name
        elif name in self.systems.configurations:
            system = self.systems.configurations[name]
        else:
            known = ", ".join(sorted(self.systems.configurations)) or "none"
            raise MachineError(f"[{self.name}] no configuration {name!r}; this guest has: {known}")
        steps: list[str] = []
        if action in ("switch", "boot"):
            steps.append(f"nix-env --profile /nix/var/nix/profiles/system --set {shlex.quote(system)}")
        steps.append(
            "systemd-run -E LOCALE_ARCHIVE -E NIXOS_INSTALL_BOOTLOADER --collect"
            " --no-ask-password --pipe --quiet --service-type=exec"
            " --unit=nixos-rebuild-switch-to-configuration --wait"
            f" {shlex.quote(system)}/bin/switch-to-configuration {action}"
        )
        done = await self.execute(
            " && ".join(steps),
            timeout=timeout,
            label=f"switch_to {name or 'booted'} {action}",
        )
        if check and done.returncode != 0:
            raise MachineError(
                f"[{self.name}] switch to {name or 'the booted system'} failed (exit {done.returncode}):\n{_both(done)}"
            )
        return done

    async def add_closure(self, info: str) -> list[str]:
        """Make a closure the host built after boot valid in the guest.

        *info* is a `closureInfo` output, such as a node's
        `system.build.vivariumNixRegistration`. Its paths go into the
        guest's store view and its registration into the guest's Nix
        database. Returns the paths that were new to the view.

        A guest with no Nix database (a cloud image guest without
        `writableStore`) sees the paths under its /nix, and that is all,
        which is enough to run a tool the host built.
        """
        added = await self.add_paths([*storeview.read_paths(Path(info) / "store-paths"), info])
        await self._load_registration(info)
        return added

    async def add_paths(self, paths: list[str]) -> list[str]:
        """Put store paths the host has into the guest's store view, and
        nothing into its Nix database: enough to run a program, such as
        the host's Python for a test in the guest. *paths* is a whole
        closure. Returns the paths that were new to the view."""
        if self.control_kind != AGENT:
            raise MachineError(f"[{self.name}] a {self.control_kind} guest has no store")
        if self._store is None:
            return []
        try:
            return await self._store.add(paths)
        except StoreError as error:
            raise MachineError(f"[{self.name}] {error}") from error

    # ── init ───────────────────────────────────────────────────────

    async def system_state(self, timeout: float = 180) -> SystemState:
        """Wait for the guest's boot to end, then how it went: the init's
        word for it (``running``, ``degraded``, ...) and what failed. systemd
        and dinit both answer."""
        return await self._ask("system_state", self._agent.system_state(timeout), timeout + _SYSTEMD_TIMEOUT)

    # ── systemd ────────────────────────────────────────────────────

    async def unit_state(self, unit: str) -> str:
        """ActiveState of *unit* (``active``, ``failed``, ...)."""
        return await self._ask(f"unit_state {unit}", self._agent.unit_state(unit), _SYSTEMD_TIMEOUT)

    async def unit_info(self, unit: str) -> dict[str, str]:
        """Every property ``systemctl show`` reports for *unit*."""
        return await self._ask(f"unit_info {unit}", self._agent.unit_info(unit), _SYSTEMD_TIMEOUT)

    async def list_units(self, pattern: str = "*") -> list[UnitInfo]:
        """Units matching *pattern*, as dicts of the systemctl columns."""
        return await self._ask(
            f"list_units {pattern}",
            self._agent.list_units(pattern),
            _SYSTEMD_TIMEOUT,
        )

    async def listening(self) -> list[int]:
        """Guest ports with something listening on them."""
        return await self._ask("listening", self._agent.listening(), _SYSTEMD_TIMEOUT)

    def interface(self, segment: str) -> Interface:
        """This guest's interface on *segment*: its name in the guest, its
        addresses and its MAC."""
        for nic in self.spec.interfaces:
            if nic.segment == segment:
                return nic
        known = ", ".join(f"{nic.name} on {nic.segment}" for nic in self.spec.interfaces) or "none"
        raise MachineError(f"[{self.name}] no interface on segment {segment!r}; it has: {known}")

    def reachable(self, guest_port: int, protocol: str = "tcp") -> list[str]:
        """Where *guest_port* answers from the host over *protocol*
        (``tcp`` or ``udp``), as ``address:port``.

        Empty when nothing forwards it -- which is the answer worth
        having, since it cannot be fixed without rebooting the guest.
        """
        return forward.reachable(self.forward, guest_port, protocol)

    async def processes(self) -> list[ProcessInfo]:
        """Every process in the guest: pid, ppid, name and cmdline.

        For proving that the thing under test left nothing running.  A
        guest is thrown away at poweroff, so it is where a leak can be
        counted with no machine to clean up afterwards.
        """
        return await self._ask("processes", self._agent.processes(), _SYSTEMD_TIMEOUT)

    async def count_processes(self, pattern: str) -> int:
        """How many processes have *pattern* in their name or command.

        Both fields, because ``/proc/<pid>/stat`` truncates a name at 15
        characters -- ``nix-daemon`` survives that and a longer name does
        not.
        """
        return sum(1 for p in await self.processes() if pattern in p["name"] or pattern in p["cmdline"])

    async def journal(self, unit: str | None = None, lines: int = 50) -> str:
        """Tail of the guest journal, optionally for one unit."""
        return await self._ask(
            f"journal {unit or 'all'}",
            self._agent.journal(unit, lines),
            _SYSTEMD_TIMEOUT,
        )

    # ── memory ─────────────────────────────────────────────────────

    async def meminfo(self) -> dict[str, int]:
        """``/proc/meminfo``, in kibibytes.

        The way to see what :meth:`shrink` achieved: the balloon holds its
        pages as ordinary allocations, so ``MemFree`` falls by what it
        took and ``MemTotal`` does not move.
        """
        text = (await self.succeed("cat /proc/meminfo", timeout=30)).stdout
        values: dict[str, int] = {}
        for line in text.splitlines():
            name, _, rest = line.partition(":")
            fields = rest.split()
            if fields and fields[0].isdigit():
                values[name] = int(fields[0])
        return values

    def host_memory_kib(self) -> int:
        """Kibibytes of host memory this guest's RAM costs right now.

        Each backend counts its own way (`Backend.host_memory_kib`). Under
        UML and QEMU a guest's memory is one sparse file, so the host pays
        for the blocks that file has allocated and nothing else. A krun or
        container guest costs what its processes map. A guest starts near
        zero however large its memory is, grows
        towards it as it touches pages, and falls again as it reports the
        pages it has freed.

        The measurement no guest-side number gives: the guest's own
        ``MemFree`` counts a page it has freed, and the host may still be
        paying for that page.
        """
        if self._process is None:
            raise MachineError(f"[{self.name}] is not running")
        # A UML kernel writes its own pid, and the backend reads it there.
        pid = self._process.guest_pid()
        if pid is None:
            raise MachineError(f"[{self.name}] no pid file: {self._process.pid_file}")
        try:
            return self.backend.host_memory_kib(self.spec, pid)
        except backends.BackendError as error:
            raise MachineError(f"[{self.name}] {error}") from error

    async def drop_caches(self) -> None:
        """Free the guest's page cache.

        Worth far more here than on a real machine, and worth it before
        every :meth:`shrink`.  The guest's cache of the store is a second
        copy of pages the host already holds, and a miss on it is a host
        ``read()`` that hits the host's own cache -- measured at 1.3-1.5
        GB/s against 5.1-6.5 GB/s for a hit, so dropping it costs a memcpy
        and not a disk read.  See issue #12.
        """
        await self.succeed("sync; echo 3 > /proc/sys/vm/drop_caches", timeout=60)

    async def shrink(self, amount: str) -> None:
        """Take *amount* of memory away from this guest.

        ``amount`` is written the way ``vivarium.memory`` is: ``"256M"``.

        A balloon, so **the pages come from what is already free**, and
        how much it gets is worth measuring rather than assuming --
        :meth:`meminfo` is where it shows, as ``MemFree`` falling. A guest
        holding a page cache gives up almost nothing until
        :meth:`drop_caches` has run.

        Both backends have a limit here and they are not the same one.
        UML's console allocates ``GFP_ATOMIC``, which cannot reclaim, and
        stops at the first page it cannot get while still reporting
        success. QEMU's balloon asks the guest, which will reclaim to
        answer and takes its time about it.

        Not the way to make the *host* stop paying: free page reporting
        already does that, on both backends, within seconds and without
        being asked. This is for squeezing a guest on purpose.
        """
        await self._balloon(-memory_bytes(amount))

    async def grow(self, amount: str) -> None:
        """Give this guest back up to *amount* of what :meth:`shrink` took.

        Never past the memory it booted with. Both backends clamp there
        rather than failing.
        """
        await self._balloon(memory_bytes(amount))

    @property
    def can_resize(self) -> bool:
        """Whether :meth:`shrink` and :meth:`grow` work on this guest.

        UML and QEMU have a control channel for it; krun and a container
        do not.
        """
        return self._controls.balloon is not None

    async def _balloon(self, delta: int) -> None:
        balloon = self._controls.balloon
        if balloon is None:
            raise MachineError(f"[{self.name}] has no control channel for its memory, so it cannot be resized")
        with self.recorder.waiting(self.name, f"balloon {delta // 1024 // 1024}M"):
            try:
                await balloon.balloon(delta)
            except ControlError as error:
                raise MachineError(f"[{self.name}] {error}") from error

    # ── screen ─────────────────────────────────────────────────────
    #
    # Through QEMU's monitor, so they work whatever the guest runs: a
    # getty, X, a Wayland compositor, another operating system. Coordinates
    # are screen pixels, (0, 0) top left, the same ones `find_text` boxes.

    @property
    def screen_size(self) -> tuple[int, int]:
        """Width and height of this guest's screen, in pixels."""
        screen = self._screen()
        return screen.width, screen.height

    def _screen(self) -> Screen:
        if (screen := self._controls.screen) is None:
            raise MachineError(
                f"[{self.name}] has no screen: set `vivarium.display.enable = true`, "
                f'which needs `vivarium.backend = "qemu"` (this guest is {self.spec.backend})'
            )
        return screen

    async def _on_screen[T](self, call: Callable[[Framebuffer], Awaitable[T]]) -> T:
        device = self._screen().device
        try:
            return await call(device)
        except ControlError as error:
            raise MachineError(f"[{self.name}] {error}") from error

    async def screenshot(self, name: str | None = None) -> Path:
        """Save the screen as PNG and return where.

        Into this guest's artifacts, under ``screenshots/``, so it is kept
        with the run's evidence. *name* without a suffix gets ``.png``;
        none numbers them in order.
        """
        base = self.artifacts or self._rundir
        if base is None:
            raise MachineError(f"[{self.name}] is not running")
        directory = base / "screenshots"
        directory.mkdir(parents=True, exist_ok=True)
        if name is None:
            name = f"{len(list(directory.glob('*.png'))):03d}"
        path = directory / (name if "." in name else f"{name}.png")
        await self._on_screen(lambda device: device.screendump(path))
        return path

    async def read_screen(self) -> list[ocr.Screen]:
        """The screen through OCR: one reading per variant, each word boxed.

        The raw screenshot first, then nixos-test's two preprocessed ones.
        Most callers want :meth:`screen_text`, :meth:`find_text` or
        :meth:`wait_for_text` instead.
        """
        screen = self._controls.screen
        tools = None if screen is None else screen.ocr
        if tools is None or self._rundir is None:
            raise MachineError(f"[{self.name}] has no OCR: it has no `vivarium.display`")
        with tempfile.TemporaryDirectory(dir=self._rundir) as scratch:
            ppm = Path(scratch) / "screen.ppm"
            await self._on_screen(lambda device: device.screendump(ppm, png=False))
            try:
                return await ocr.read(ppm, *tools)
            except ocr.OcrError as error:
                raise MachineError(f"[{self.name}] {error}") from error

    async def screen_text(self) -> str:
        """What the screen says, as OCR reads the screenshot unprocessed."""
        return (await self.read_screen())[0].text

    async def find_text(self, pattern: str | re.Pattern[str]) -> list[ocr.Text]:
        """Where *pattern* is on the screen now, in the first reading that
        has it. Empty if no reading does."""
        for screen in await self.read_screen():
            if found := screen.find(pattern):
                return found
        return []

    async def wait_for_text(self, pattern: str | re.Pattern[str], timeout: float = 60) -> ocr.Text:
        """Wait until *pattern* is on the screen; return where it is.

        On timeout the error carries every reading of the last screen and
        a screenshot of it, because OCR misreads and the reason is usually
        visible in what it read instead.
        """
        deadline = time.monotonic() + timeout
        with self.recorder.waiting(self.name, f"text {pattern!s}"):
            while True:
                screens = await self.read_screen()
                for screen in screens:
                    if found := screen.find(pattern):
                        return found[0]
                if time.monotonic() > deadline:
                    shot = await self.screenshot()
                    read = "\n---\n".join(screen.text for screen in screens)
                    raise MachineError(
                        f"[{self.name}] timed out waiting for {pattern!s} on the screen ({shot}); OCR read:\n{read}"
                    )
                await anyio.sleep(0.5)

    async def send_key(self, *keys: str, delay: float = 0.01) -> None:
        """Press and release each of *keys* in turn.

        QEMU's ``sendkey`` names, ``-`` joining a chord: ``"ret"``,
        ``"ctrl-alt-f2"``, ``"shift-tab"``, ``"meta_l-d"``.
        """
        for key in keys:
            await self._on_screen(lambda device, key=key: device.sendkey(key))
            await anyio.sleep(delay)

    async def send_chars(self, text: str, delay: float = 0.01) -> None:
        r"""Type *text* on a US layout; ``"\n"`` is Enter."""
        await self.send_key(*(display.key_for(char) for char in text), delay=delay)

    async def move(self, x: int, y: int) -> None:
        """Put the pointer on pixel (*x*, *y*)."""
        width, height = self.screen_size
        events = display.move_events(x, y, width, height)
        await self._on_screen(lambda device: device.input(events))

    async def press(self, button: display.Button | str = display.Button.LEFT) -> None:
        """Hold a pointer button down, where the pointer is."""
        events = display.button_events(display.Button(button), True)
        await self._on_screen(lambda device: device.input(events))

    async def release(self, button: display.Button | str = display.Button.LEFT) -> None:
        events = display.button_events(display.Button(button), False)
        await self._on_screen(lambda device: device.input(events))

    async def click(self, x: int, y: int, button: display.Button | str = display.Button.LEFT, count: int = 1) -> None:
        """Click at (*x*, *y*); *count* 2 is a double click."""
        await self.move(x, y)
        for _ in range(count):
            await self.press(button)
            await self.release(button)
            await anyio.sleep(0.05)

    async def drag(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        button: display.Button | str = display.Button.LEFT,
        steps: int = 10,
    ) -> None:
        """Press at *start*, move to *end* in *steps*, release."""
        await self.move(*start)
        await self.press(button)
        for step in range(1, steps + 1):
            await self.move(
                round(start[0] + (end[0] - start[0]) * step / steps),
                round(start[1] + (end[1] - start[1]) * step / steps),
            )
            await anyio.sleep(0.01)
        await self.release(button)

    async def scroll(self, x: int, y: int, clicks: int) -> None:
        """Turn the wheel at (*x*, *y*): positive scrolls down."""
        button = display.Button.WHEEL_DOWN if clicks > 0 else display.Button.WHEEL_UP
        await self.move(x, y)
        for _ in range(abs(clicks)):
            await self.press(button)
            await self.release(button)

    async def click_text(
        self,
        pattern: str | re.Pattern[str],
        timeout: float = 60,
        button: display.Button | str = display.Button.LEFT,
    ) -> ocr.Text:
        """Wait for *pattern* on the screen and click its middle."""
        found = await self.wait_for_text(pattern, timeout)
        await self.click(*found.center, button=button)
        return found

    def waiting(self, what: str) -> contextlib.AbstractContextManager[None]:
        """Record a wait of the test's own as one step.

        `wait_for_unit` and friends are timed already.  A loop a test
        writes itself is not, and on a long test that is most of the run:
        measured on nixkube's nine scenarios, 632 of 786 seconds were in
        settle loops the runner could not see.  Wrap one and it appears::

            with cp.waiting("the node's state to settle"):
                await settle(cp)
        """
        return self.recorder.waiting(self.name, what)

    async def wait_for_unit(self, unit: str, timeout: float = 120) -> None:
        """Wait until *unit* is active, failing fast if it dies first."""
        with self.recorder.waiting(self.name, f"unit {unit}"):
            await self._wait_for_unit(unit, timeout)

    async def _wait_for_unit(self, unit: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            state = await self.unit_state(unit)
            if state == "active":
                return
            if state == "failed":
                raise MachineError(f"[{self.name}] unit {unit} failed:\n{await self.journal(unit)}")
            if time.monotonic() > deadline:
                # With the journal, like the `failed` branch above: an
                # `inactive` unit says only that nothing happened, and why
                # is in the log of whatever was to pull it in.
                raise MachineError(
                    f"[{self.name}] timed out waiting for unit {unit} (state: {state})\n{await self.journal(unit)}"
                )
            await anyio.sleep(0.5)

    async def wait_until_succeeds(self, command: str, timeout: float = 120, poll: float = 1.0) -> CommandResult:
        """Run *command* in the guest until it exits 0; return how it ended.

        The nixos-test idiom for a settle: push the condition into the
        command -- a ``grep -q``, a ``ping`` -- so a wait is a command that
        comes good. Raises after *timeout* seconds, with the last output.
        """
        out = ""

        async def succeeded() -> CommandResult | None:
            nonlocal out
            try:
                done = await self.execute(command)
            except GuestTimeout as error:
                # One slow attempt: the wait goes on, as after an exit 1.
                out = str(error)
                return None
            out = _both(done)
            return done if done.returncode == 0 else None

        async def last() -> str:
            return out

        return await self.until(f"`{command}` to succeed", succeeded, timeout, poll, evidence=last)

    async def wait_until_fails(self, command: str, timeout: float = 120, poll: float = 1.0) -> CommandResult:
        """Run *command* in the guest until it exits non-zero; return how it ended.

        A withdrawal: a route that goes, a peer that stops answering. An
        attempt that times out raises `GuestTimeout`: it did not exit.
        """

        async def failed() -> CommandResult | None:
            done = await self.execute(command)
            return done if done.returncode != 0 else None

        return await self.until(f"`{command}` to fail", failed, timeout, poll)

    async def until[T](
        self,
        what: str,
        check: Callable[[], Awaitable[T | None]],
        timeout: float = 120,
        poll: float = 1.0,
        evidence: Callable[[], Awaitable[str]] | None = None,
    ) -> T:
        """Call *check* until it returns something other than ``None``; return that.

        The settle for a parsed result, where `wait_until_succeeds` reads
        an exit code: *check* parses a guest's state and returns ``None``
        while it is not there yet. ``False`` and ``{}`` are results.
        After *timeout* seconds it raises, with what *evidence* returns.
        """
        with self.waiting(what):
            try:
                # Bounds *check* itself: a deadline tested between calls lets
                # a slow check overshoot it by its own timeouts. Measured on
                # the k8s recipe: a 300s wait still ran at 291s, last checked
                # at 258s, and two kubectl timeouts could take it to 540s.
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
        """Call *check* for *timeout* seconds; raise if it ever returns something other than ``None``.

        A pass always takes the whole *timeout*: hold a negative only as
        long as the thing it guards against needs to show up.
        """
        started = time.monotonic()
        with self.waiting(f"never {what}"), anyio.move_on_after(timeout):
            while True:
                value = await check()
                if value is not None:
                    raise MachineError(
                        f"[{self.name}] {what}: happened after {time.monotonic() - started:.0f}s:\n{value}"
                    )
                await anyio.sleep(poll)
