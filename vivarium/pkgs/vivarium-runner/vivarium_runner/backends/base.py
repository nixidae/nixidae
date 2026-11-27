"""What a backend is: the protocol `Machine` drives, and the launch config a backend reads."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..capability import Controls

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..boot import Boot
    from ..machine import Machine
    from ..machine_spec import MachineSpec
    from ..needs import Missing
    from ..store import Store


class BackendError(Exception):
    """A guest could not be assembled: a helper that would not start."""


def launch_config(
    spec: MachineSpec, backend: str, fields: frozenset[str], optional: frozenset[str] = frozenset()
) -> Mapping[str, object]:
    """`spec.launch.config`, checked against the fields *backend* reads.

    *fields* must all be there; *optional* may be.

    A missing field or one the backend does not know is an error that
    names the guest and the option: a misspelled field otherwise starts
    the guest without it.
    """
    owner = f"{spec.name}: vivarium.launch"
    if spec.launch is None:
        raise BackendError(f"{owner} is not set; the {backend} backend's module sets it")
    if spec.launch.backend != backend:
        raise BackendError(f"{owner}.backend is {spec.launch.backend!r}, but the guest runs on {backend!r}")
    given = set(spec.launch.config)
    if unknown := sorted(given - fields - optional):
        raise BackendError(f"{owner}.config has fields the {backend} backend does not know: {', '.join(unknown)}")
    if missing := sorted(fields - given):
        raise BackendError(f"{owner}.config lacks fields the {backend} backend needs: {', '.join(missing)}")
    return spec.launch.config


def config_section(
    owner: str, value: object, fields: frozenset[str], optional: frozenset[str] = frozenset()
) -> Mapping[str, Any]:
    """A table inside `launch.config`, checked as `launch_config` checks the whole."""
    if not isinstance(value, Mapping):
        raise BackendError(f"{owner} is not an attribute set")
    given = set(value)
    if unknown := sorted(given - fields - optional):
        raise BackendError(f"{owner} has fields the backend does not know: {', '.join(unknown)}")
    if missing := sorted(fields - given):
        raise BackendError(f"{owner} lacks fields the backend needs: {', '.join(missing)}")
    return value


def with_boot_image(spec: MachineSpec, kernel: Path) -> MachineSpec:
    """*spec* booting *kernel* in place of `vivarium.boot.kernel.image`."""
    if spec.boot is None:
        raise BackendError(f"{spec.name}: no vivarium.boot")
    try:
        return replace(spec, boot=spec.boot.with_image(kernel))
    except ValueError as error:
        raise BackendError(f"{spec.name}: {error}") from error


def config_path(config: Mapping[str, object], key: str) -> Path:
    return Path(str(config[key]))


def config_list(owner: str, value: object) -> list[object]:
    if not isinstance(value, list):
        raise BackendError(f"{owner} is not a list")
    return value


def config_maybe_path(config: Mapping[str, object], key: str) -> Path | None:
    return config_path(config, key) if key in config else None


@dataclass(frozen=True)
class Stop:
    """How a guest's process ended, as the host saw it."""

    returncode: int | None
    cause: str | None = None
    """QEMU's `ShutdownCause`, such as `guest-reset`, when its monitor said one."""


@dataclass
class Launch:
    """One guest's process, and whatever has to outlive its start."""

    argv: list[str]
    pass_fds: tuple[int, ...]
    env: dict[str, str] = field(default_factory=dict)
    helpers: list[subprocess.Popen[bytes]] = field(default_factory=list)
    """Side processes the guest needs -- virtiofsd, the egress.  Killed
    when the guest is torn down, in this order."""
    controls: Controls = field(default_factory=Controls)
    """What the guest's process offers beside its agent. With `power`, the
    guest's CPUs wait for `Machine` to start them."""
    pid_file: Path | None = None
    """Where the guest wrote its own pid. A UML kernel writes one."""
    cleanup: list[Path] = field(default_factory=list)
    """Directories the backend made outside the run directory, removed when
    the guest is torn down."""
    agent_path: Path | None = None
    """A socket to connect to for the agent, once it says it is ready, for
    a backend with no serial line to carry the socketpair."""
    disk_fds: tuple[int, ...] = ()
    """The root disk, for a backend that boots the guest again after a
    reboot. The runner keeps these open and hands them back, so the disk
    outlives the process that wrote it."""
    notes: list[str] = field(default_factory=list)
    """Console lines that say how the guest reaches out: the uplink's DNS
    and whether it is offline. The machine says them under its name."""


class Backend(Protocol):
    """What `Machine` asks of a backend.

    `boot` and `disk_fds` are for a reboot, which `Qemu` and `Krun` take
    (`reboots`); the other backends accept them and ignore them. A first
    boot boots `spec.boot`.
    """

    name: str
    reboots: bool
    has_kernel: bool
    """The guest runs its own kernel: it may drop its caches, and its
    overlay keeps a negative dentry for a store path it looked up before
    the path existed."""

    def rebooted(self, stop: Stop) -> bool:
        """Whether a guest process that ended asked for a reboot."""
        ...

    def host_memory_kib(self, spec: MachineSpec, pid: int) -> int:
        """Kibibytes of host memory the guest whose own process is *pid* costs now."""
        ...

    def store(self, rundir: Path, paths: list[str]) -> Store:
        """The guest's view of *paths* in the host's store, made in *rundir*."""
        ...

    def validate(self, spec: MachineSpec) -> None:
        """Raise `BackendError` when `spec.launch` is wrong for this backend."""
        ...

    async def missing(self, specs: Sequence[MachineSpec]) -> list[Missing]:
        """What this host lacks to run *specs*, beyond their declared needs
        (`vivarium_runner.needs`); asked before any guest boots."""
        ...

    def with_kernel(self, spec: MachineSpec, kernel: Path) -> MachineSpec:
        """*spec*, booting *kernel* instead of the kernel Nix built.

        Raise `BackendError` when the runner names no kernel for this guest.
        """
        ...

    async def launch(
        self,
        machine: Machine,
        rundir: Path,
        agent_fd: int,
        lan_fds: dict[str, int],
        boot: Boot | None = None,
        disk_fds: tuple[int, ...] = (),
    ) -> Launch: ...
