"""The in-guest half of the control channel.

Started by the ``vivarium-agent`` systemd unit, this serves arpyc on a serial
line -- the other end of which is a socketpair held by the host runner.
Commands therefore work before (and without) any guest networking,
including inside a Nix build sandbox.

Commands run with ``/run/current-system/sw/bin`` on ``PATH``, so whatever
a test's NixOS config puts in ``environment.systemPackages`` is callable.
A command runs in a thread, so the channel still carries other calls
while it runs.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

import anyio
import anyio.to_thread
from dinit_client import DinitClient
from dinit_client.protocol import ServiceState, ServiceStatus, ShutdownType, StoppedReason

from . import cloud
from .arpyc import AsyncConnection, Service, listen
from .channel import AGENT_READY, ProcessInfo, SystemState, UnitInfo


class Init(StrEnum):
    """PID 1, as the unit or service that starts the agent names it."""

    SYSTEMD = "systemd"
    DINIT = "dinit"


TTY = os.environ.get("VIVARIUM_AGENT_DEVICE", "/dev/ttyS0")
"""Where the host is listening.  ttyS0 under UML.  Under QEMU the console
takes ttyS0 -- a stock kernel prints there from its first line, before any
virtio driver exists -- and the agent gets hvc0 instead."""
INIT = Init(os.environ.get("VIVARIUM_INIT", Init.SYSTEMD))
DINIT_SOCKET = os.environ.get("VIVARIUM_DINIT_SOCKET", "/run/dinitctl")
DINIT_BOOT = "boot"
"""The service dinix makes depend on, or wait for, every other one."""
_DINIT_FAILED = frozenset(
    {StoppedReason.DEPFAILED, StoppedReason.FAILED, StoppedReason.EXECFAILED, StoppedReason.TIMEDOUT}
)

GUEST_PATH = os.environ.get("VIVARIUM_GUEST_PATH", "/run/current-system/sw/bin:/run/current-system/sw/sbin")
"""Another distribution's guest has no /run/current-system, and sets its
own; see modules/image-guest.nix."""


KILL_WAIT = 5.0
"""Seconds to read what a killed command left in its pipes. Well inside the
10 s the host adds over a command's timeout, so the host hears the timeout."""


OOM_SCORE_ADJ = Path("/proc/self/oom_score_adj")
OOM_PROTECTED = -1000

_AT_DEFAULT_OOM_SCORE = '{ echo 0 > /proc/self/oom_score_adj; } 2>/dev/null; exec /bin/sh -c "$1"'
"""The shell around a command: it leaves the agent's protected OOM score,
then runs the command unchanged. A test's workload must stay killable, and
`preexec_fn` is not safe in the agent's threads."""


def _sh(command: str, timeout: float, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run *command* in a process group of its own.

    At *timeout* the whole group gets SIGKILL, so a pipeline or a child in
    the background stops too, and a builtin `TimeoutError` carries the
    output so far to the host.
    """
    with subprocess.Popen(  # noqa: S603 -- running the host's command is the agent's job
        ["/bin/sh", "-c", _AT_DEFAULT_OOM_SCORE, "sh", command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        process_group=0,
        # PATH last: the guest's own is what makes `environment.systemPackages`
        # callable, and a caller that sets PATH here would break every later
        # command in ways that read as a missing package. `dict(a, **b)` cannot
        # do this -- a `PATH` in *env* would collide with the keyword.
        env={**os.environ, **(env or {}), "PATH": GUEST_PATH},
    ) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Empty when only a descendant that left the group holds the pipes.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            try:
                out, err = proc.communicate(timeout=KILL_WAIT)
            except subprocess.TimeoutExpired:
                # A descendant that left the group still holds the pipes.
                out, err = "", f"(no output: a process outside the group held it open past {KILL_WAIT:g}s)"
            said = (out + err).rstrip("\n")
            raise TimeoutError(f"ran past its {timeout:g}s timeout; its process group got SIGKILL\n{said}") from None
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


async def _sh_thread(command: str, timeout: float) -> subprocess.CompletedProcess[str]:
    """`_sh` in a thread, so the channel goes on answering while it runs."""
    return await anyio.to_thread.run_sync(_sh, command, timeout, None)


def _systemd(call: str) -> None:
    if INIT is not Init.SYSTEMD:
        raise RuntimeError(f"{call} asks systemd, and this guest's init is {INIT}")


def _dinit_failed(status: ServiceStatus) -> bool:
    if status.state is not ServiceState.STOPPED:
        return False
    # A process that ended by itself: dinit says TERMINATED whatever its
    # status, so only an exit 0 is clean (dinitctl: "terminated; exited -
    # status 1").
    if status.stop_reason is StoppedReason.TERMINATED:
        # si_code and si_status from protocol 5, exit_status before it.
        clean = (status.si_code, status.si_status) == (os.CLD_EXITED, 0) or status.exit_status == 0
        return not clean
    return status.stop_reason in _DINIT_FAILED


async def _dinit_state(timeout: float) -> SystemState:
    """dinit's answer to `exposed_system_state`: the boot service started,
    gave up or is still starting, and every service that stopped for a failure."""
    async with DinitClient.connect(DINIT_SOCKET) as client:
        # Before the status: the handle it opens is what makes dinit send
        # boot's events, so no change falls between the read and the wait.
        events = client.subscribe()
        boot = await client.status(DINIT_BOOT)
        with anyio.move_on_after(timeout):
            while boot.state is not ServiceState.STARTED and boot.target_state is ServiceState.STARTED:
                await events.receive()
                boot = await client.status(DINIT_BOOT)
        services = await client.list_services()
    failed = tuple(sorted(name for name, status in services.items() if _dinit_failed(status)))
    if boot.state is not ServiceState.STARTED:
        return {"state": "starting" if boot.target_state is ServiceState.STARTED else "failed", "failed": failed}
    return {"state": "degraded" if failed else "running", "failed": failed}


class Agent(Service):
    """What the host may ask this guest to do."""

    def on_disconnect(self, conn: AsyncConnection) -> None:
        print("vivarium-agent: host disconnected", flush=True)

    async def exposed_run(
        self,
        command: str,
        timeout: float = 900,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        """Run a shell command; returns (exit code, stdout, stderr), each
        stream without its trailing newlines.

        *env* is added to this one command's environment.  The same
        argument on both backends -- UML could pass a variable on the
        kernel command line and QEMU has no equivalent, so neither does.

        In a thread, so the channel goes on reading while it runs: a
        command that calls the host's cloud API waits on a reply this
        channel carries (nixidae/nixidae#1408).

        Past *timeout* it raises `TimeoutError`; see `_sh`.
        """
        done = await anyio.to_thread.run_sync(_sh, command, timeout, env)
        return done.returncode, done.stdout.rstrip("\n"), done.stderr.rstrip("\n")

    async def exposed_system_state(self, timeout: float = 180) -> SystemState:
        """Wait up to *timeout* for the boot to end, then say how it went."""
        if INIT is Init.DINIT:
            return await _dinit_state(timeout)
        try:
            state = (await _sh_thread("systemctl is-system-running --wait", timeout)).stdout.strip()
        except TimeoutError:
            # `--wait` returns once the boot ends, so only a boot that did
            # not end is still `starting`.
            state = "starting"
        if state == "running":
            return {"state": state, "failed": ()}
        units = await _sh_thread("systemctl list-units --state=failed --no-legend --plain", 30)
        return {"state": state, "failed": tuple(line.split()[0] for line in units.stdout.splitlines() if line.strip())}

    async def exposed_poweroff(self) -> None:
        """Begin an orderly power-off. It returns before the guest is down."""
        if INIT is Init.DINIT:
            async with DinitClient.connect(DINIT_SOCKET) as client:
                await client.shutdown(ShutdownType.POWEROFF)
            return
        await _sh_thread("systemctl poweroff", 30)

    async def exposed_unit_state(self, unit: str) -> str:
        """ActiveState of *unit*, or ``unknown`` if systemd won't say."""
        _systemd("unit_state")
        out = (await _sh_thread(f"systemctl show --property=ActiveState -- {unit!r}", 30)).stdout
        _, sep, state = out.strip().partition("ActiveState=")
        return state if sep else "unknown"

    async def exposed_unit_info(self, unit: str) -> dict[str, str]:
        """Every property ``systemctl show`` reports for *unit*."""
        _systemd("unit_info")
        out = (await _sh_thread(f"systemctl show --no-pager -- {unit!r}", 30)).stdout
        pairs = (line.partition("=") for line in out.strip().splitlines())
        return {key: value for key, sep, value in pairs if sep}

    async def exposed_list_units(self, pattern: str = "*") -> list[UnitInfo]:
        """Units matching *pattern*, one dict per systemctl column."""
        _systemd("list_units")
        out = (await _sh_thread(f"systemctl list-units --all --no-legend -- {pattern!r}", 30)).stdout
        units: list[UnitInfo] = []
        for line in out.splitlines():
            fields = line.split(maxsplit=4)
            if len(fields) >= 4:
                name, load, active, sub, *rest = fields
                units.append(
                    {
                        "name": name,
                        "load": load,
                        "active": active,
                        "sub": sub,
                        "description": rest[0] if rest else "",
                    }
                )
        return units

    def exposed_listening(self) -> list[int]:
        """Ports this guest has a TCP socket listening on.

        Read out of ``/proc/net/tcp`` rather than asked of ``ss``,
        because it works whatever the guest has installed.  The forwards
        themselves are `vivarium.inbound`, so this is here to say what is
        reachable and what came up somewhere nothing is listening for it.
        """
        ports: set[int] = set()
        for family in ("tcp", "tcp6"):
            try:
                lines = Path(f"/proc/net/{family}").read_text().splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                fields = line.split()
                # st == 0A is TCP_LISTEN; local_address is HEXADDR:HEXPORT.
                if len(fields) > 3 and fields[3] == "0A":
                    ports.add(int(fields[1].rsplit(":", 1)[1], 16))
        return sorted(ports)

    def exposed_processes(self) -> list[ProcessInfo]:
        """Every process in this guest: pid, ppid, name and command line.

        Out of ``/proc`` and not asked of ``ps``, for the reason
        :meth:`exposed_listening` reads ``/proc/net/tcp``: a guest that
        installs no procps still answers.

        ``name`` is ``stat``'s comm, so the first 15 characters of the
        executable's name and nothing longer.  Match on ``cmdline`` for
        anything named longer than that.
        """
        found: list[ProcessInfo] = []
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                stat = Path(f"/proc/{entry.name}/stat").read_text()
                raw = Path(f"/proc/{entry.name}/cmdline").read_bytes()
            except OSError:
                # It exited between the listing and the read, which is
                # the answer: it is not running.
                continue
            # comm is in brackets and may hold spaces and brackets of its
            # own, so split on the last one rather than on whitespace.
            head, _, rest = stat.rpartition(")")
            name = head.partition("(")[2]
            fields = rest.split()
            found.append(
                {
                    "pid": int(entry.name),
                    "ppid": int(fields[1]) if len(fields) > 1 else 0,
                    "name": name,
                    "cmdline": raw.replace(b"\0", b" ").decode(errors="replace").strip(),
                }
            )
        return sorted(found, key=lambda p: p["pid"])

    async def exposed_journal(self, unit: str | None = None, lines: int = 50) -> str:
        """Tail of the journal, optionally restricted to one unit."""
        _systemd("journal")
        scope = f"-u {unit!r}" if unit else ""
        return (await _sh_thread(f"journalctl --no-pager -n {lines:d} {scope}", 30)).stdout.rstrip()


UNIX_PREFIX = "unix:"
"""A :data:`TTY` that starts with this names a socket path instead.  A
container has no serial line: the agent listens on a socket in a
directory the host binds in, and the host connects once it sees
:data:`AGENT_READY`."""


PORT_PREFIX = "virtio-port:"
"""A :data:`TTY` that starts with this names a virtio-serial port. A guest
with no udev has no ``/dev/virtio-ports/<name>`` link, so the agent finds the
port by the name sysfs gives it, as udev's rule does."""

VIRTIO_PORTS = Path("/sys/class/virtio-ports")


def virtio_port(name: str, ports: Path = VIRTIO_PORTS) -> Path:
    """The device node of the virtio-serial port called *name*."""
    for port in sorted(ports.iterdir()):
        if (port / "name").read_text().strip() == name:
            return Path("/dev") / port.name
    raise FileNotFoundError(f"no virtio-serial port is named {name!r} in {ports}")


def _accept_one(path: str) -> int:
    Path(path).unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    # Listening before the ready line, so the host's connect never races
    # the bind.
    print(AGENT_READY, flush=True)
    conn, _ = server.accept()
    server.close()
    return conn.detach()


async def _serve() -> None:
    if TTY.startswith(UNIX_PREFIX):
        fd = await anyio.to_thread.run_sync(_accept_one, TTY[len(UNIX_PREFIX) :])
        conn = listen(fd, Agent(), raw_tty=False)
    else:
        device = virtio_port(TTY[len(PORT_PREFIX) :]) if TTY.startswith(PORT_PREFIX) else Path(TTY)
        fd = os.open(device, os.O_RDWR)
        # A krun guest's channel is a virtio-console port that is not a
        # console, so it has no line discipline to make raw.
        conn = listen(fd, Agent(), raw_tty=os.isatty(fd))
        print(AGENT_READY, flush=True)
    async with anyio.create_task_group() as group:
        # `vivarium.cloud`: the guest calls the host's cloud API through this channel.
        if os.environ.get("VIVARIUM_CLOUD") == "1":
            await group.start(cloud.serve, conn)
        await conn.serve_forever()
        group.cancel_scope.cancel()


def main() -> None:
    # The OOM killer takes something else first: a killed agent reads as a
    # hung guest. A container guest's user namespace refuses a negative
    # score, and the agent then runs at the default.
    with contextlib.suppress(PermissionError):
        OOM_SCORE_ADJ.write_text(str(OOM_PROTECTED))
    anyio.run(_serve)


if __name__ == "__main__":
    main()
