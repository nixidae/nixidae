"""The krun backend: a guest as a libkrun microVM."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass, replace
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

from .. import forward, unixpath
from ..lifetime import die_with_parent
from ..spec import UplinkProxy
from ..store import AllowList
from .base import BackendError, Launch, Stop, config_maybe_path, config_path, launch_config
from .host import (
    RELAY_IN_PORT,
    abandon,
    egress_adds,
    egress_args,
    log_tail,
    memory_bytes,
    processes_pss_kib,
    require_ids,
    scratch_disk,
    socket_dir,
    start_egress,
)

if TYPE_CHECKING:
    from ..boot import Boot, Kernel
    from ..machine import Machine
    from ..machine_spec import MachineSpec
    from ..store import Store


@dataclass(frozen=True)
class KrunConfig:
    """`vivarium.launch.config` under krun."""

    krun: Path
    """`vivarium-krun`, the process a krun guest runs as."""
    qemu_img: Path
    gvproxy: Path | None
    """For `vivarium.uplink.proxy = "gvproxy"`."""
    acpi: bool
    """libkrun describes the guest's devices in ACPI, and ends on its S5.
    Its ACPI tables are x86 only."""
    egress: Path | None = None
    """The router's host half, on the unix sockets libkrun's vsock reaches."""

    @classmethod
    def of(cls, spec: MachineSpec) -> KrunConfig:
        config = launch_config(spec, "krun", frozenset({"krun", "qemuImg", "acpi"}), frozenset({"gvproxy", "egress"}))
        if spec.vsock and "egress" not in config:
            raise BackendError(f"{spec.name}: a vsock uplink needs egress in vivarium.launch.config")
        return cls(
            krun=config_path(config, "krun"),
            qemu_img=config_path(config, "qemuImg"),
            gvproxy=config_maybe_path(config, "gvproxy"),
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

    @staticmethod
    async def _gvproxy(
        gvproxy: Path | None,
        rundir: Path,
        spec: MachineSpec,
        rules: list[forward.Rule],
        helpers: list[subprocess.Popen[bytes]],
        opened: list[int],
    ) -> int:
        """Start gvproxy for one guest's uplink; return the guest's end.

        gvproxy goes into *helpers* and the guest's end into *opened* as
        each one exists, so the launch that owns them cleans up after a
        failure or a cancel here.

        gvproxy takes no fd: it listens on a socket in *rundir*, and the
        runner connects. It frames with a 4-byte length prefix, so
        `vivarium-krun --uplink` takes the fd unchanged. The config is
        always written: without one gvproxy forwards 127.0.0.1:2222 and
        two guests collide on it. The addresses are the uplink's fixed
        ones (nix/uplink.nix), as the relay gives.

        Under `--offline` gvproxy runs in the session's own network
        namespace and reaches nothing.
        """
        if gvproxy is None:
            raise BackendError(f"{spec.name}: vivarium.launch.config has no gvproxy")
        sock = rundir / "gvproxy.sock"
        if not unixpath.fits(sock):
            raise BackendError(f"{spec.name}: gvproxy's socket path is too long for AF_UNIX: {sock}")
        network = spec.uplink_network
        forwards: dict[str, str] = {}
        for rule in rules:
            if rule.protocols != ("tcp",):
                raise BackendError(f"{spec.name}: gvproxy forwards TCP ports only, not {rule}")
            for port in rule.ports:
                forwards[f"{rule.address}:{port.host}"] = f"{network.address}:{port.guest}"
        config = {
            "stack": {
                "mtu": spec.mtu,
                "subnet": network.subnet,
                "gatewayIP": network.gateway,
                "dhcpStaticLeases": {network.address: spec.mac(0)},
                "forwards": forwards,
            },
            "interfaces": {"qemu": f"unix://{sock}"},
        }
        config_path = rundir / "gvproxy.yaml"
        # JSON is YAML, and gvproxy reads YAML.
        config_path.write_text(json.dumps(config))
        log = rundir / "gvproxy.log"
        with log.open("wb") as handle:
            proc = subprocess.Popen(
                [str(gvproxy), "-config", str(config_path)],
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                preexec_fn=die_with_parent,
            )
        helpers.append(proc)
        # The connect is the readiness check. gvproxy's ready notification
        # comes before its qemu listener exists.
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        opened.append(client.fileno())
        try:
            with anyio.move_on_after(5):
                while proc.poll() is None:
                    try:
                        client.connect(str(sock))
                    except (FileNotFoundError, ConnectionRefusedError):
                        await anyio.sleep(0.01)
                    else:
                        return client.fileno()
        finally:
            # `opened` owns the fd: a close here as well would close
            # whatever reused its number.
            client.detach()
        raise BackendError(f"{spec.name}: gvproxy did not serve {sock} (exit {proc.poll()}):\n{log_tail(log)}")

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
        uplink_fds: tuple[int, ...] = ()
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
            elif spec.uplink_proxy == UplinkProxy.GVPROXY:
                gvproxy_fd = await Krun._gvproxy(config.gvproxy, rundir, spec, machine.forward, helpers, opened)
                uplink_fds = (gvproxy_fd,)
                uplink = [f"--uplink={gvproxy_fd},{spec.mac(0)}"]
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
        if spec.nested:
            argv.append("--nested")
        if config.acpi:
            argv.append("--acpi")
        return Launch(
            argv=argv,
            pass_fds=(agent_fd, *uplink_fds, disk_rw, *lan_fds.values()),
            helpers=helpers,
            disk_fds=(disk_rw,),
            cleanup=cleanup,
            notes=notes,
        )
