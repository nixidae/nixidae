"""The krun backend: a guest as a libkrun microVM."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, replace
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING

from .. import forward
from ..store import AllowList
from .base import BackendError, Launch, Stop, config_maybe_path, config_path, launch_config
from .host import (
    RELAY_IN_PORT,
    abandon,
    egress_adds,
    egress_args,
    kept_store,
    memory_bytes,
    processes_pss_kib,
    require_ids,
    scratch_disk,
    socket_dir,
    start_egress,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..boot import Boot, Kernel
    from ..machine import Machine
    from ..machine_spec import MachineSpec
    from ..needs import Missing
    from ..store import Store


@dataclass(frozen=True)
class KrunConfig:
    """`vivarium.launch.config` under krun."""

    krun: Path
    """`vivarium-krun`, the process a krun guest runs as."""
    qemu_img: Path
    acpi: bool
    """libkrun describes the guest's devices in ACPI, and ends on its S5.
    Its ACPI tables are x86 only."""
    egress: Path | None = None
    """The router's host half, on the unix sockets libkrun's vsock reaches."""

    @classmethod
    def of(cls, spec: MachineSpec) -> KrunConfig:
        config = launch_config(spec, "krun", frozenset({"krun", "qemuImg", "acpi"}), frozenset({"egress"}))
        if spec.vsock and "egress" not in config:
            raise BackendError(f"{spec.name}: a vsock uplink needs egress in vivarium.launch.config")
        return cls(
            krun=config_path(config, "krun"),
            qemu_img=config_path(config, "qemuImg"),
            acpi=bool(config["acpi"]),
            egress=config_maybe_path(config, "egress"),
        )


class KrunExit(IntEnum):
    """The exit status of a krun guest's process: the code the guest set
    through libkrun's virtio-fs ioctl before its reset. `krunExit` in
    modules/qemu.nix sets these."""

    POWEROFF = 0
    CRASH = 70
    REBOOT = 71


RELAY_PORT = 5000
"""Where the router's relay connects for each flow: its VSOCK_PORT, in modules/router.nix."""


DAX_WINDOW = 512 << 20
"""Bytes of guest physical address space the store is mapped through.

Address space, not RAM: the host pays for the page cache the guest maps,
whatever the window's size. A window smaller than what a guest reads
makes the guest unmap and remap ranges; 512 MiB held a whole 362 MiB
closure without that (nixidae/nixidae#1077).

Not larger for free: the guest builds a page struct for every page of
the window in its own RAM, 1/64 of the window's size. That is 8 MiB at
512 MiB and 64 MiB at 4 GiB."""


class Krun:
    """A guest as a libkrun microVM, with KVM.

    The kernel, initrd and disk are QEMU's. The
    store is served from inside the guest's own process, with a DAX window,
    so the guest maps the host's page cache of it instead of reading it
    into its own RAM. No monitor: no screen, and no balloon to resize.
    """

    name = "krun"
    reboots = True
    """As `Qemu.reboots`. Every stop is a reset, so the exit code says which
    one it was: `KrunExit`."""
    has_kernel = True

    @staticmethod
    def rebooted(stop: Stop) -> bool:
        return stop.returncode == KrunExit.REBOOT

    @staticmethod
    def host_memory_kib(spec: MachineSpec, pid: int) -> int:
        # libkrun's RAM is anonymous, so there is no file to count. PSS
        # also holds this guest's share of the host page cache it maps
        # through the DAX window, which is the store pages it reads.
        return processes_pss_kib(pid)

    @staticmethod
    def store(rundir: Path, paths: list[str]) -> Store:
        return AllowList.write(rundir / "store-allowlist", paths)

    @staticmethod
    async def missing(specs: Sequence[MachineSpec]) -> list[Missing]:
        del specs  # the declared needs say it all
        return []

    @staticmethod
    def validate(spec: MachineSpec) -> None:
        KrunConfig.of(spec)
        Krun._kernel(spec, spec.boot)

    @staticmethod
    def _kernel(spec: MachineSpec, boot: Boot | None) -> tuple[Boot, Kernel, Path]:
        """*boot*, its kernel, and that kernel's ELF, which is all libkrun loads."""
        if boot is None or boot.kernel is None:
            raise BackendError(f"{spec.name}: a krun guest needs vivarium.boot.kernel")
        if boot.firmware is not None:
            raise BackendError(f"{spec.name}: a krun guest boots no firmware")
        if boot.root is not None:
            raise BackendError(f"{spec.name}: a krun guest boots no guest kind's root image")
        if boot.kernel.vmlinux is None:
            raise BackendError(
                f"{spec.name}: the guest's bootloader names kernel {boot.kernel.image}, "
                "and a krun guest boots only the kernel it started with"
            )
        return boot, boot.kernel, boot.kernel.vmlinux

    @staticmethod
    def with_kernel(spec: MachineSpec, kernel: Path) -> MachineSpec:
        # `image` stays: a reboot compares the bootloader's choice with it.
        boot, own, _ = Krun._kernel(spec, spec.boot)
        return replace(spec, boot=replace(boot, kernel=replace(own, vmlinux=kernel)))

    async def launch(
        self,
        machine: Machine,
        rundir: Path,
        agent_fd: int,
        lan_fds: dict[str, int],
        boot: Boot | None = None,
        disk_fds: tuple[int, ...] = (),
    ) -> Launch:
        # macOS has no user namespaces, and its libkrun keeps a file's
        # owner in an xattr.
        if sys.platform == "linux":
            require_ids(machine.spec.name, "krun")
        spec = machine.spec
        config = KrunConfig.of(spec)
        boot, kernel, vmlinux = Krun._kernel(spec, boot or spec.boot)
        opened: list[int] = []
        helpers: list[subprocess.Popen[bytes]] = []
        disk_ro: int | None = None
        cleanup: list[Path] = []
        notes: list[str] = []
        try:
            # A reboot keeps the disk the guest wrote. libkrun opens it by
            # path and takes no lock, so the runner's fd serves again.
            if disk_fds:
                disk_rw = disk_fds[0]
            else:
                disk_rw, disk_ro = await scratch_disk(config.qemu_img, rundir, spec.image)
                opened += [disk_rw, disk_ro]
            # As Qemu._uplink_netdev: an uplink segment (a router's,
            # nixidae/nixidae#1404) is the run's fd.
            if spec.vsock:
                # No uplink: libkrun's own vsock carries each relay flow to
                # the egress's socket, and listens for its forwards (E2).
                # A router network's relay has a port and a socket of its
                # own: the socket tells the egress the flow's network.
                names = {port: f"egress-{port}.sock" for port in spec.network_ports}
                sockets, cleanup = socket_dir(rundir, max(["relay.sock", *names.values()], key=len), None)
                listen, relay = sockets / "egress.sock", sockets / "relay.sock"
                networks = {port: sockets / name for port, name in names.items()}
                # A reboot finds the last boot's sockets, and neither side binds over one.
                for path in (listen, relay, *networks.values()):
                    path.unlink(missing_ok=True)
                args = [*egress_args(machine), *(f"--network={path}" for path in networks.values())]
                machine.egress_control = await start_egress(
                    config.egress,
                    rundir,
                    spec.uplink_network.gateway,
                    str(listen),
                    listen,
                    args,
                    egress_adds(machine, str(relay)),
                    helpers,
                )
                machine.egress_relay = str(relay)
                uplink = [
                    f"--vsock={RELAY_PORT},{listen}",
                    f"--vsock-listen={RELAY_IN_PORT},{relay}",
                    *(f"--vsock={port},{path}" for port, path in networks.items()),
                ]
                notes = forward.egress_notes(machine.spec.uplink_network, machine.offline)
            elif spec.uplink is not None:
                uplink = [f"--uplink-segment={lan_fds['vec0']},{spec.mac(0)}"]
            else:
                raise BackendError(f"{spec.name}: nothing serves vec0 under krun: uplink.proxy is {spec.uplink_proxy}")
        except BaseException:
            abandon(helpers, opened)
            raise
        # libkrun opens the disk by path, and only the fd keeps the unlinked
        # file alive. It opens it read-write and needs no read-only twin.
        if disk_ro is not None:
            os.close(disk_ro)

        argv = [
            str(config.krun),
            f"--vmlinux={vmlinux}",
            f"--cmdline={boot.cmdline(kernel)}",
            *([f"--initrd={kernel.initrd}"] if kernel.initrd is not None else []),
            f"--cpus={spec.cpus}",
            f"--memory={memory_bytes(spec.memory) >> 20}",
            f"--disk={'/proc/self' if sys.platform == 'linux' else '/dev'}/fd/{disk_rw}",
            f"--store={spec.store}",
            f"--dax-window={DAX_WINDOW}",
            f"--agent-fd={agent_fd}",
            *uplink,
            *(f"--lan={lan_fds[nic.name]},{nic.mac}" for nic in spec.interfaces),
        ]
        if spec.store_allowlist is not None:
            argv.append(f"--store-allowlist={spec.store_allowlist}")
        if machine.artifacts is not None:
            argv.append(f"--artifacts={machine.artifacts}")
        if (kept := kept_store(machine, rundir)) is not None:
            argv.append(f"--kept={kept}")
        if spec.nested:
            argv.append("--nested")
        if config.acpi:
            argv.append("--acpi")
        return Launch(
            argv=argv,
            pass_fds=(agent_fd, disk_rw, *lan_fds.values()),
            helpers=helpers,
            disk_fds=(disk_rw,),
            cleanup=cleanup,
            notes=notes,
        )
