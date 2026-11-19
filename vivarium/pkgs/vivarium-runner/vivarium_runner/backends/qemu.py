"""The QEMU backend: a guest as a KVM virtual machine."""

from __future__ import annotations

import itertools
import os
import shutil
import socket
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .. import forward, qmp
from ..boot import RootFormat
from ..capability import Controls, Screen
from ..lifetime import die_with_parent
from ..store import BoundStore
from .base import (
    BackendError,
    Launch,
    Stop,
    config_list,
    config_maybe_path,
    config_path,
    config_section,
    launch_config,
    with_boot_image,
)
from .host import (
    RELAY_IN_PORT,
    abandon,
    alive,
    egress_adds,
    egress_args,
    log_tail,
    memory_file_kib,
    require_ids,
    scratch_disk,
    socket_dir,
    start_egress,
    start_passt,
)

if TYPE_CHECKING:
    from ..boot import Boot
    from ..machine import Machine
    from ..machine_spec import MachineSpec
    from ..store import Store


@dataclass(frozen=True)
class BlankDisk:
    """One of `launch.config.disks` under QEMU: an empty raw disk that lasts the run."""

    name: str
    """The disk's serial, so the guest finds it as /dev/disk/by-id/virtio-<name>."""
    size: int
    """In MiB."""

    @classmethod
    def of(cls, owner: str, value: object) -> BlankDisk:
        disk = config_section(owner, value, frozenset({"name", "size"}))
        return cls(name=str(disk["name"]), size=int(disk["size"]))


@dataclass(frozen=True)
class QemuConfig:
    """`vivarium.launch.config` under QEMU."""

    qemu: Path
    qemu_img: Path
    virtiofsd: Path
    passt: Path
    ocr: tuple[Path, Path] | None
    """tesseract and magick, for a guest with `vivarium.display`."""
    config_disk: Path | None = None
    """A second disk, copied writable: RouterOS's configuration."""
    uplink: bool = True
    """False: no passt uplink, so the first segment is the first NIC of a
    guest kind's own root image."""
    egress: Path | None = None
    """The router's host half, which serves its vhost-user vsock device."""
    swtpm: Path | None = None
    """A TPM 2.0 for the guest: `vivarium.tpm`."""
    disks: tuple[BlankDisk, ...] = ()

    @classmethod
    def of(cls, spec: MachineSpec) -> QemuConfig:
        config = launch_config(
            spec,
            "qemu",
            frozenset({"qemu", "qemuImg", "virtiofsd", "passt"}),
            frozenset({"tesseract", "magick", "configDisk", "uplink", "egress", "swtpm", "disks"}),
        )
        if spec.vsock and "egress" not in config:
            raise BackendError(f"{spec.name}: a vsock uplink needs egress in vivarium.launch.config")
        tesseract, magick = config_maybe_path(config, "tesseract"), config_maybe_path(config, "magick")
        if (tesseract is None) != (magick is None):
            raise BackendError(f"{spec.name}: vivarium.launch.config names one of tesseract and magick; OCR needs both")
        disks = f"{spec.name}: vivarium.launch.config.disks"
        return cls(
            qemu=config_path(config, "qemu"),
            qemu_img=config_path(config, "qemuImg"),
            virtiofsd=config_path(config, "virtiofsd"),
            passt=config_path(config, "passt"),
            ocr=(tesseract, magick) if tesseract is not None and magick is not None else None,
            config_disk=config_maybe_path(config, "configDisk"),
            uplink=bool(config.get("uplink", True)),
            egress=config_maybe_path(config, "egress"),
            swtpm=config_maybe_path(config, "swtpm"),
            disks=tuple(
                BlankDisk.of(f"{disks}.{index}", disk)
                for index, disk in enumerate(config_list(disks, config.get("disks", [])))
            ),
        )


ARTIFACTS_TAG = "artifacts"
"""The virtiofs tag QEMU serves the same directory under.
``modules/qemu.nix`` mounts it by this name."""


def _drive_args(disk_fds: tuple[int, ...], drives: list[tuple[str, str | None]]) -> list[str]:
    """QEMU's arguments for each *drive*, a format and a serial, on its pair of *disk_fds*.

    By fd rather than by name -- see `scratch_disk`. Two fds in each
    set, because QEMU opens an image O_RDONLY to probe its format before it
    opens it O_RDWR, and takes the fd whose access mode matches.

    Each drive is an explicit `-device`, so the guest numbers them in this
    order, and the root is /dev/vda, the name `fileSystems."/"` gives it.
    """
    args: list[str] = []
    for index, (form, serial) in enumerate(drives):
        fdset = index + 1
        args += [
            "-add-fd",
            f"fd={disk_fds[2 * index]},set={fdset}",
            "-add-fd",
            f"fd={disk_fds[2 * index + 1]},set={fdset}",
            "-drive",
            f"file=/dev/fdset/{fdset},if=none,id=drive{index},format={form}",
            "-device",
            f"virtio-blk-pci,drive=drive{index}" + (f",serial={serial}" if serial is not None else ""),
        ]
    return args


def _reopen(disk_fds: tuple[int, ...]) -> tuple[int, ...]:
    """New open file descriptions of the disks a reboot keeps.

    QEMU's image lock is an OFD lock: it belongs to the open file
    description, which outlives the QEMU that took it. So a reboot opens
    the unlinked image afresh and lets the old descriptions go.
    """
    return tuple(
        os.open(f"/proc/self/fd/{fd}", flags) for fd, flags in zip(disk_fds, itertools.cycle((os.O_RDWR, os.O_RDONLY)))
    )


def _monitor(path: Path) -> int:
    """QEMU's monitor socket, listening before QEMU starts.

    So the runner connects as soon as QEMU runs, with no wait for the
    socket to appear. A guest that rebooted left the previous one's path.
    """
    path.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        listener.listen(1)
    except BaseException:
        listener.close()
        raise
    return listener.detach()


def _monitor_args(fd: int) -> list[str]:
    """The monitor on *fd*, and the guest's CPUs stopped until the runner
    starts them through it: see `Qmp.run`."""
    return [
        "-chardev",
        f"socket,id=monitor,fd={fd},server=on,wait=off",
        "-object",
        "monitor-qmp,id=qmp,chardev=monitor",
        "-S",
    ]


def _lan_args(spec: MachineSpec, lan_fds: Mapping[str, int]) -> list[str]:
    """QEMU's netdev and device for each LAN interface, on its socket fd."""
    argv: list[str] = []
    for nic in spec.interfaces:
        argv += [
            "-netdev",
            f"dgram,id=nic{nic.nic},local.type=fd,local.str={lan_fds[nic.name]}",
            "-device",
            f"virtio-net-pci,netdev=nic{nic.nic},mac={nic.mac},host_mtu={nic.mtu}",
        ]
    return argv


class Qemu:
    """A guest as a virtual machine, with KVM.

    Needs ``/dev/kvm``: ``accel=kvm`` and never ``accel=kvm:tcg``, because
    the fallback is silent and ten times slower, so a builder that lost KVM
    would only look like a slow day.
    """

    name = "qemu"
    reboots = True
    """A reboot inside the guest boots it again, into the system its
    bootloader names. See `Machine._reboot`."""
    has_kernel = True

    @staticmethod
    def rebooted(stop: Stop) -> bool:
        # A reset with `-no-reboot` ends QEMU. Linux's reboot is one, and
        # so is a reset the firmware or the bootloader makes before Linux.
        return stop.cause == "guest-reset"

    @staticmethod
    def host_memory_kib(spec: MachineSpec, pid: int) -> int:
        # The guest's RAM is a `memory-backend-memfd`.
        return memory_file_kib(pid, spec.memory)

    @staticmethod
    def store(rundir: Path, paths: list[str]) -> Store:
        return BoundStore.build(rundir / "nix", paths)

    def validate(self, spec: MachineSpec) -> None:
        QemuConfig.of(spec)
        boot = spec.boot
        # Firmware boots its first system straight from the kernel too: see `Boot`.
        if boot is None or (boot.kernel is None and boot.root is None):
            raise BackendError(f"{spec.name}: vivarium.boot names no kernel and no root image to boot")
        if boot.root is not None and boot.root.format is RootFormat.SQUASHFS:
            raise BackendError(f"{spec.name}: QEMU boots no squashfs root image")

    def with_kernel(self, spec: MachineSpec, kernel: Path) -> MachineSpec:
        return with_boot_image(spec, kernel)

    async def launch(
        self,
        machine: Machine,
        rundir: Path,
        agent_fd: int,
        lan_fds: dict[str, int],
        boot: Boot | None = None,
        disk_fds: tuple[int, ...] = (),
    ) -> Launch:
        disk_boot = machine.spec.boot is not None and machine.spec.boot.root is not None
        # A disk boot has no virtiofsd.
        if not disk_boot:
            require_ids(machine.spec.name, "QEMU")
        helpers: list[subprocess.Popen[bytes]] = []
        opened: list[int] = []
        try:
            if disk_boot:
                return await self._launch_disk(machine, rundir, agent_fd, lan_fds, helpers, opened, boot, disk_fds)
            return await self._launch(machine, rundir, agent_fd, lan_fds, helpers, opened, boot, disk_fds)
        except BaseException:
            abandon(helpers, opened)
            raise

    async def _uplink_netdev(
        self,
        machine: Machine,
        rundir: Path,
        lan_fds: dict[str, int],
        helpers: list[subprocess.Popen[bytes]],
        opened: list[int],
    ) -> tuple[str, int | None]:
        """The `vec0` QEMU netdev, and the passt fd to keep open (or None).

        passt by default. When the spec names an uplink segment, vec0 is a
        dgram on that segment's fd instead, so the uplink joins an
        out-of-band management network a peer serves -- there is no passt,
        and so no forwards (nixidae/nixidae#958).
        """
        spec = machine.spec
        if spec.uplink is not None:
            return f"dgram,id=vec0,local.type=fd,local.str={lan_fds['vec0']}", None
        passt_fd = await start_passt(
            QemuConfig.of(spec).passt, rundir, spec.uplink_network, machine.forward, machine.offline, helpers, opened
        )
        return f"stream,id=vec0,addr.type=fd,addr.str={passt_fd}", passt_fd

    async def _launch(
        self,
        machine: Machine,
        rundir: Path,
        agent_fd: int,
        lan_fds: dict[str, int],
        helpers: list[subprocess.Popen[bytes]],
        opened: list[int],
        boot: Boot | None = None,
        disk_fds: tuple[int, ...] = (),
    ) -> Launch:
        spec = machine.spec
        config = QemuConfig.of(spec)

        # Every unix socket this guest needs, in one directory short
        # enough to name them: virtiofsd's, the artifacts one, and the
        # monitor, and a router's vsock. `vfs`, `art` and `qmp` are no
        # longer than `vsock`, so one check covers all four.
        sockets, cleanup = socket_dir(rundir, "vsock", None)
        monitor_fd = _monitor(sockets / "qmp")
        opened.append(monitor_fd)

        vfs_fd = await self._virtiofsd(config.virtiofsd, sockets, "vfs", spec.store, helpers, opened)

        art_fd: int | None = None
        if machine.artifacts is not None:
            art_fd = await self._virtiofsd(config.virtiofsd, sockets, "art", str(machine.artifacts), helpers, opened)

        # A reboot keeps the disk the guest wrote, and the runner owns it:
        # see `_reopen`.
        boot = boot or spec.boot
        if boot is None:
            raise BackendError(f"{spec.name}: no vivarium.boot")
        firmware = boot.firmware
        # The root first, so it is /dev/vda: then the ESP, then the blank disks.
        drives: list[tuple[str, str | None]] = [("qcow2", None)]
        if firmware is not None:
            drives.append(("qcow2", None))
        drives += [("raw", disk.name) for disk in config.disks]
        if disk_fds:
            disk_fds = _reopen(disk_fds)
            opened.extend(disk_fds)
        else:
            disk_fds = await scratch_disk(config.qemu_img, rundir, spec.image)
            opened.extend(disk_fds)
            if firmware is not None:
                esp_fds = await scratch_disk(config.qemu_img, rundir, firmware.esp, backing="qcow2")
                opened.extend(esp_fds)
                disk_fds += esp_fds
            for disk in config.disks:
                blank_fds = self._blank_disk(rundir, disk)
                opened.extend(blank_fds)
                disk_fds += blank_fds
        if len(disk_fds) != 2 * len(drives):
            raise BackendError(f"{spec.name}: {len(disk_fds)} disk fds for {len(drives)} disks")

        tpm_fd: int | None = None
        if config.swtpm is not None:
            tpm_fd = await self._swtpm(config.swtpm, rundir, helpers, opened)

        passt_fd: int | None = None
        if spec.vsock:
            # No vec0 from the host: the guest reaches it over vsock, a
            # vhost-user device the egress serves, as on UML. A reboot
            # leaves the socket.
            device = sockets / "vsock"
            device.unlink(missing_ok=True)
            machine.egress_control = await start_egress(
                config.egress,
                rundir,
                spec.uplink_network.gateway,
                f"vhost-user:{device}",
                device,
                egress_args(machine),
                egress_adds(machine, f"vsock:{RELAY_IN_PORT}"),
                helpers,
            )
            uplink = ["-chardev", f"socket,id=vsock,path={device}", "-device", "vhost-user-vsock-pci,chardev=vsock"]
            notes = forward.egress_notes(machine.spec.uplink_network, machine.offline)
        else:
            netdev, passt_fd = await self._uplink_netdev(machine, rundir, lan_fds, helpers, opened)
            notes = forward.uplink_notes(machine.spec.uplink_network, machine.offline) if passt_fd is not None else []
            uplink = ["-netdev", netdev, "-device", f"virtio-net-pci,netdev=vec0,mac={spec.mac(0)},host_mtu={spec.mtu}"]

        argv = [
            str(config.qemu),
            "-machine",
            "q35,accel=kvm,memory-backend=guest-memory",
            # Nested virtualization only when the guest asked: `-cpu host`
            # alone hands every guest the host's vmx or svm, and udev
            # then loads KVM in it. See `nestedVirtualization`.
            "-cpu",
            "host" if spec.nested else "host,-vmx,-svm",
            "-smp",
            str(spec.cpus),
            "-m",
            spec.memory,
            "-nodefaults",
            "-no-reboot",
            "-display",
            "none",
            # vhost-user-fs reads the guest's RAM directly, so the RAM has
            # to be a shared memfd rather than anonymous.  Without this,
            # virtiofsd connects and every read returns nothing.
            "-object",
            f"memory-backend-memfd,id=guest-memory,size={spec.memory},share=on",
            *_drive_args(disk_fds, drives),
            # The console, read by ConsoleLines.
            "-serial",
            "stdio",
            "-chardev",
            f"socket,id=virtiofs,fd={vfs_fd}",
            "-device",
            "vhost-user-fs-pci,chardev=virtiofs,tag=nix",
            # The control channel: the same socketpair UML gets on ssl0.
            "-chardev",
            f"socket,id=agent,fd={agent_fd}",
            "-device",
            "virtio-serial",
            "-device",
            "virtserialport,chardev=agent,name=vivarium-agent" if firmware is not None else "virtconsole,chardev=agent",
            *uplink,
            # The guest tells QEMU which pages it has freed and QEMU
            # madvises them out of the memfd above, so the host stops
            # paying for a page cache the guest has dropped. Without it a
            # guest drifts towards its whole `-m` and stays there.
            "-device",
            "virtio-balloon-pci,free-page-reporting=on",
            # And the monitor, which is how the host asks for a size
            # rather than waiting for the guest to volunteer one.
            *_monitor_args(monitor_fd),
        ]
        # A firmware boot has no kernel: systemd-boot on the ESP picks one.
        if (kernel := boot.kernel) is not None:
            argv += ["-kernel", str(kernel.image), "-append", boot.cmdline(kernel)]
            if kernel.initrd is not None:
                argv += ["-initrd", str(kernel.initrd)]
        if firmware is not None:
            # The variables outlive each QEMU, as NVRAM outlives a reset:
            # bootctl's boot entry and systemd-boot's LoaderEntryDefault are
            # there.
            variables = rundir / "efivars.fd"
            if not variables.exists():
                shutil.copyfile(firmware.variables, variables)
                variables.chmod(0o600)
            argv += [
                "-drive",
                f"if=pflash,format=raw,unit=0,readonly=on,file={firmware.code}",
                "-drive",
                f"if=pflash,format=raw,unit=1,file={variables}",
            ]
        if (display := spec.display) is not None:
            argv += [
                # `-display none` stays: screendump reads the device's
                # surface, not a window.
                "-device",
                f"virtio-gpu-pci,xres={display.width},yres={display.height}",
                # An absolute pointer, so a click lands where it is aimed
                # without the guest's acceleration in between. The
                # keyboard is q35's own PS/2, which `sendkey` drives.
                "-device",
                "virtio-tablet-pci",
            ]
            # q35's VMware mouse is absolute too, and the guest's driver
            # makes it current: measured, every `abs` event went to it and
            # the tablet saw none.
            argv[argv.index("-machine") + 1] += ",vmport=off"
        if firmware is not None and firmware.smm:
            argv[argv.index("-machine") + 1] += ",smm=on"
            argv += ["-global", "driver=cfi.pflash01,property=secure,value=on"]
        if tpm_fd is not None:
            argv += [
                "-chardev",
                f"socket,id=tpm,fd={tpm_fd}",
                "-tpmdev",
                "emulator,id=tpm,chardev=tpm",
                "-device",
                "tpm-tis,tpmdev=tpm",
            ]
        if art_fd is not None:
            argv += [
                "-chardev",
                f"socket,id=artifacts,fd={art_fd}",
                "-device",
                f"vhost-user-fs-pci,chardev=artifacts,tag={ARTIFACTS_TAG}",
            ]
        argv += _lan_args(spec, lan_fds)

        pass_fds = tuple(
            fd
            for fd in (agent_fd, passt_fd, *lan_fds.values(), vfs_fd, art_fd, tpm_fd, monitor_fd, *disk_fds)
            if fd is not None
        )
        return Launch(
            argv=argv,
            pass_fds=pass_fds,
            helpers=helpers,
            controls=Qemu._controls(spec, sockets / "qmp"),
            cleanup=cleanup,
            disk_fds=disk_fds,
            notes=notes,
        )

    async def _launch_disk(
        self,
        machine: Machine,
        rundir: Path,
        agent_fd: int,
        lan_fds: dict[str, int],
        helpers: list[subprocess.Popen[bytes]],
        opened: list[int],
        boot: Boot | None = None,
        disk_fds: tuple[int, ...] = (),
    ) -> Launch:
        """Another operating system from its own disk: its own bootloader
        and kernel, or `boot.kernel`, and none of the store, the agent or
        virtiofs.

        The serial port is the control channel, the end of the socketpair
        the agent would get: `control.ConsoleControl` drives it, or the
        Machine reads it as console. The uplink is the first NIC, so it is
        the OS's first interface; without it, the first segment is.
        """
        spec = machine.spec
        config = QemuConfig.of(spec)
        boot = boot or spec.boot
        if boot is None or boot.root is None:
            raise BackendError(f"{spec.name}: vivarium.boot has no root image to boot")
        kernel = boot.kernel
        sockets, cleanup = socket_dir(rundir, "qmp", None)

        disk_fds = (
            _reopen(disk_fds) if disk_fds else await scratch_disk(config.qemu_img, rundir, spec.image, boot.root.format)
        )
        opened.extend(disk_fds)

        argv = [
            str(config.qemu),
            "-machine",
            "q35,accel=kvm",
            "-cpu",
            "host,-vmx,-svm",
            "-smp",
            str(spec.cpus),
            "-m",
            spec.memory,
            "-nodefaults",
            "-no-reboot",
            "-display",
            "none",
            "-add-fd",
            f"fd={disk_fds[0]},set=1",
            "-add-fd",
            f"fd={disk_fds[1]},set=1",
            "-drive",
            "file=/dev/fdset/1,if=none,id=root,format=qcow2",
            # Pinned: a second disk otherwise competes for the boot. Not
            # with `-kernel`, which takes boot index 0 itself.
            "-device",
            "virtio-blk-pci,drive=root" + ("" if kernel is not None else ",bootindex=0"),
            "-chardev",
            f"socket,id=control,fd={agent_fd}",
            "-serial",
            "chardev:control",
        ]
        passt_fd: int | None = None
        if config.uplink:
            uplink, passt_fd = await self._uplink_netdev(machine, rundir, lan_fds, helpers, opened)
            argv += ["-netdev", uplink, "-device", f"virtio-net-pci,netdev=vec0,mac={spec.mac(0)}"]
        monitor_fd = _monitor(sockets / "qmp")
        opened.append(monitor_fd)
        argv += _monitor_args(monitor_fd)
        if kernel is not None:
            argv += ["-kernel", str(kernel.image), "-append", boot.cmdline(kernel)]
            if kernel.initrd is not None:
                argv += ["-initrd", str(kernel.initrd)]
        if (config_disk := config.config_disk) is not None:
            # A copy, writable: RouterOS does not mount a read-only disk.
            # A reboot keeps what the guest wrote to it.
            copy = rundir / "config.img"
            if not copy.exists():
                shutil.copyfile(config_disk, copy)
                copy.chmod(0o600)
            argv += ["-drive", f"file={copy},if=virtio,format=raw"]
        argv += _lan_args(spec, lan_fds)
        pass_fds = tuple(fd for fd in (agent_fd, passt_fd, *lan_fds.values(), monitor_fd, *disk_fds) if fd is not None)
        return Launch(
            argv=argv,
            pass_fds=pass_fds,
            helpers=helpers,
            controls=Qemu._controls(spec, sockets / "qmp"),
            cleanup=cleanup,
            disk_fds=disk_fds,
            notes=forward.uplink_notes(machine.spec.uplink_network, machine.offline) if passt_fd is not None else [],
        )

    @staticmethod
    def _controls(spec: MachineSpec, path: Path) -> Controls:
        """The monitor at *path*: the balloon, the power switch and, with `vivarium.display`, the screen.

        One client for all of them: the monitor serves one connection.
        """
        monitor = qmp.Qmp(path)
        shown = spec.display
        screen = None if shown is None else Screen(monitor, shown.width, shown.height, QemuConfig.of(spec).ocr)
        return Controls(channel=monitor, balloon=monitor, power=monitor, screen=screen)

    @staticmethod
    def _blank_disk(rundir: Path, disk: BlankDisk) -> tuple[int, int]:
        """An empty raw disk of *disk*'s size, unlinked as `scratch_disk`'s is.

        Sparse, so the host pays only for the blocks the guest writes.
        """
        path = rundir / f"disk-{disk.name}.raw"
        rw = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.ftruncate(rw, disk.size << 20)
            ro = os.open(path, os.O_RDONLY)
        except BaseException:
            os.close(rw)
            raise
        finally:
            path.unlink()
        return rw, ro

    @staticmethod
    async def _virtiofsd(
        virtiofsd: Path,
        rundir: Path,
        socket_name: str,
        shared: str,
        helpers: list[subprocess.Popen[bytes]],
        opened: list[int],
    ) -> int:
        """Serve a host directory, and leave no socket behind; return QEMU's end.

        *socket_name* keeps the store's and the artifacts' sockets apart
        under *rundir*. virtiofsd goes into *helpers* and QEMU's end into
        *opened*, as `start_passt` does.

        virtiofsd takes a *listening* socket on ``--fd``, so the path it
        was bound to is only needed for long enough to connect to it once.
        Bind, listen, connect, unlink, and hand one end to virtiofsd and
        the other to QEMU: nothing is left in the filesystem even while the
        guest is running, so nothing can be left after it.


        ``--no-announce-submounts`` is load-bearing, and the failure it
        avoids names nothing useful.  NixOS binds ``/nix/store`` onto
        itself, so ``store`` is a submount of the shared directory.
        Announced, the guest makes it an automount dentry, and overlayfs
        refuses one as a lower layer -- ``ovl_dentry_weird`` rejects
        ``DCACHE_NEED_AUTOMOUNT`` and every lookup under ``/nix/store``
        fails with ``EREMOTE``.  What that reads as, from the initrd, is
        ``Failed to resolve path ... : Object is remote``, on a store the
        guest can list one directory above.

        The cost of turning it off: the guest sees one inode number space
        across what were two filesystems on the host.  Two host
        filesystems sharing an inode number would alias.  That cannot
        happen while ``/nix/store`` is a bind of ``/nix``.
        """
        # Short, because an AF_UNIX path is about 107 bytes and a run
        # directory under a long TMPDIR eats most of that.
        socket_path = rundir / socket_name
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(socket_path))
            listener.listen(1)
            client.connect(str(socket_path))
        except OSError as error:
            listener.close()
            client.close()
            raise BackendError(f"could not make a socket at {socket_path}: {error}") from error
        finally:
            socket_path.unlink(missing_ok=True)
        client_fd = client.detach()
        opened.append(client_fd)

        with listener:
            proc = subprocess.Popen(
                [
                    str(virtiofsd),
                    f"--fd={listener.fileno()}",
                    f"--shared-dir={shared}",
                    # Nothing left to drop: this is already unprivileged, and
                    # namespace sandboxing needs privileges a build does not
                    # have.
                    "--sandbox",
                    "none",
                    "--cache",
                    "auto",
                    "--no-announce-submounts",
                ],
                stdin=subprocess.DEVNULL,
                pass_fds=(listener.fileno(),),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                preexec_fn=die_with_parent,
            )
        helpers.append(proc)

        # virtiofsd serves the runner's listener, so no socket file appears
        # to wait for: the readiness check is that it is still alive a
        # moment later. Without it, a bad argument or an unreadable shared
        # directory would show up as a guest whose initrd cannot mount /nix,
        # which names the wrong thing.
        if not await alive(proc):
            raise BackendError(f"virtiofsd exited ({proc.returncode}) instead of serving {shared}")
        return client_fd

    @staticmethod
    async def _swtpm(swtpm: Path, rundir: Path, helpers: list[subprocess.Popen[bytes]], opened: list[int]) -> int:
        """Start a TPM 2.0 on one end of a socketpair; return QEMU's end.

        One swtpm for each boot, over a state directory that lasts the run.
        So a reboot resets the PCRs and keeps the TPM's NV storage, as a
        power cycle does. swtpm ends when QEMU closes its end.
        """
        state = rundir / "tpm"
        state.mkdir(mode=0o700, exist_ok=True)
        qemu_end, tpm_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        qemu_fd = qemu_end.detach()
        opened.append(qemu_fd)
        log = rundir / "swtpm.log"
        with tpm_end, log.open("ab") as handle:
            proc = subprocess.Popen(
                [
                    str(swtpm),
                    "socket",
                    "--tpm2",
                    "--tpmstate",
                    f"dir={state}",
                    "--ctrl",
                    f"type=unixio,clientfd={tpm_end.fileno()},terminate",
                ],
                stdin=subprocess.DEVNULL,
                pass_fds=(tpm_end.fileno(),),
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                preexec_fn=die_with_parent,
            )
        helpers.append(proc)
        if not await alive(proc):
            raise BackendError(f"swtpm exited ({proc.returncode}) instead of serving the TPM:\n{log_tail(log)}")
        return qemu_fd
