"""The UML backend: a guest as an ordinary Linux process."""

from __future__ import annotations

import mmap
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

from .. import forward, mconsole
from ..boot import RootFormat
from ..capability import Controls
from ..store import BoundStore
from .base import BackendError, Launch, Stop, config_maybe_path, launch_config, with_boot_image
from .host import (
    RELAY_IN_PORT,
    abandon,
    egress_adds,
    egress_args,
    kept_store,
    memory_bytes,
    memory_file_kib,
    socket_dir,
    start_egress,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..boot import Boot, Kernel, Root
    from ..machine import Machine
    from ..machine_spec import MachineSpec
    from ..needs import Missing
    from ..store import Store


@dataclass(frozen=True)
class UmlConfig:
    """`vivarium.launch.config` under UML."""

    qemu_img: Path | None
    """For a cloud image, which is qcow2 and which ubd cannot read."""
    seed: Path | None = None
    """A cloud image's seed disk, read-only: cloud-init finds it by its label."""
    egress: Path | None = None
    """The router's host half, which serves its vsock device."""

    @classmethod
    def of(cls, spec: MachineSpec) -> UmlConfig:
        config = launch_config(spec, "uml", frozenset(), frozenset({"qemuImg", "seed", "egress"}))
        if spec.vsock and "egress" not in config:
            raise BackendError(f"{spec.name}: a vsock uplink needs egress in vivarium.launch.config")
        return cls(
            qemu_img=config_maybe_path(config, "qemuImg"),
            seed=config_maybe_path(config, "seed"),
            egress=config_maybe_path(config, "egress"),
        )


ARTIFACTS_ENV = "VIVARIUM_ARTIFACTS"
"""What the UML guest's /init reads its host directory from: one
guest's subdirectory of the run's artifacts."""


STORE_ENV = "VIVARIUM_STORE"
"""What the UML guest's /init mounts as its /nix: the guest's store view,
or the host's /nix when it has none."""


_VECTOR_DEPTH = 64
"""Frames per ``sendmmsg``/``recvmmsg``, and NAPI's poll weight, for UML.

Also how many receive buffers the driver keeps allocated per interface,
each one MTU-sized -- so at a jumbo MTU this is megabytes of the guest's
RAM.  There is no point going deep: AF_UNIX lets about ten frames sit in
a socketpair, so nothing beyond that is ever in flight."""


PHYSMEM_DIRS = ("/dev/shm", "/tmp")
"""Where a UML guest's RAM may live, best first.

UML's "physical" memory is an unlinked file it mmaps ``MAP_SHARED``, put
in ``TMPDIR`` when that is set and in ``/dev/shm`` or ``/tmp`` when it is
not.  Nix sets ``TMPDIR`` to the build directory in every sandbox, and
UML takes it even though it is on the builder's disk -- it only warns,
``Warning: tempdir /build is not on tmpfs``.  Every page the guest
dirties is then a dirty page of a disk file, so the host writes the
guest's RAM out under ``vm.dirty_ratio``.  Measured on a guest that
dirtied 468 MiB of its own RAM and exited: 84-90 MiB reached the disk
with ``TMPDIR`` on btrfs, 12-13 MiB with it on tmpfs."""


def _fstype(path: str) -> str | None:
    """What kind of filesystem *path* is on, from ``/proc/self/mountinfo``.

    The longest mount point that is a prefix of *path* wins, and the last
    such line wins a tie -- a later mount over the same directory hides
    the earlier one.
    """
    found: str | None = None
    longest = -1
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        before, _, after = line.partition(" - ")
        fields, rest = before.split(), after.split()
        if len(fields) < 5 or not rest:
            continue
        point = fields[4]
        if path != point and not path.startswith(point.rstrip("/") + "/"):
            continue
        if len(point) >= longest:
            longest, found = len(point), rest[0]
    return found


def _fits_guest_ram(directory: str, size: int) -> bool:
    """Can *size* bytes of guest RAM live in *directory*?

    Three questions, each one UML asks itself at boot and two of which it
    answers by carrying on anyway.  ``statfs`` for the kind, ``statvfs``
    for the room, and a ``PROT_EXEC`` mapping of a file in it -- that
    last one is ``check_tmpexec``, and a ``noexec`` tmpfs makes the guest
    ``exit(1)`` before it prints anything else.

    The room asked for is the whole of ``mem=``, although the file is
    sparse and a guest rarely touches all of it.  Being wrong in this
    direction costs the disk-backed behaviour we already have; being
    wrong the other way costs a guest killed part way through a test.
    """
    if _fstype(directory) != "tmpfs":
        return False
    try:
        stat = os.statvfs(directory)
    except OSError:
        return False
    if stat.f_bavail * stat.f_frsize < size:
        return False
    try:
        with tempfile.TemporaryFile(dir=directory) as probe:
            probe.write(b"\0" * mmap.PAGESIZE)
            probe.flush()
            with mmap.mmap(
                probe.fileno(),
                mmap.PAGESIZE,
                flags=mmap.MAP_PRIVATE,
                prot=mmap.PROT_READ | mmap.PROT_EXEC,
            ):
                return True
    except (OSError, ValueError):
        return False


def physmem_dir(memory: str) -> str | None:
    """Where this guest's RAM should live, or ``None`` to leave it to UML.

    ``None`` is the honest answer on a host with no usable tmpfs: UML
    then does what it does today, which is slower but works.
    """
    size = memory_bytes(memory)
    for directory in PHYSMEM_DIRS:
        if _fits_guest_ram(directory, size):
            return directory
    return None


def processors(kernel: Kernel, cpus: int) -> tuple[list[str], list[str]]:
    """The kernel arguments for `cpus` processors, and notes for the user.

    Without `ncpus=` an SMP kernel brings up one processor, or with
    pkgs/uml-kernel's 0004 patch all the host's but one.
    """
    if kernel.smp:
        return [f"ncpus={cpus}"], []
    if cpus > 1:
        return [], [
            f"{cpus} processors asked, but this UML kernel is not SMP, so one runs;"
            " vivarium.uml.smp builds an SMP kernel"
        ]
    return [], []


VIRTIO_ID_VSOCK = 19
"""`virtio_uml.device=SOCKET:ID`: the virtio device ID of vsock."""


class Uml:
    """A guest as an ordinary Linux process.

    Asks nothing of the host: no ``/dev/kvm``, no root, no tap device. One
    processor, and a trap into the host kernel for every guest syscall.
    """

    name = "uml"
    reboots = False
    has_kernel = True

    @staticmethod
    def rebooted(stop: Stop) -> bool:
        return False

    @staticmethod
    def host_memory_kib(spec: MachineSpec, pid: int) -> int:
        # The kernel maps an unlinked temporary file as the guest's RAM.
        return memory_file_kib(pid, spec.memory)

    @staticmethod
    def store(rundir: Path, paths: list[str]) -> Store:
        return BoundStore.build(rundir / "nix", paths)

    @staticmethod
    async def missing(specs: Sequence[MachineSpec]) -> list[Missing]:
        del specs  # the declared needs say it all
        return []

    @staticmethod
    def validate(spec: MachineSpec) -> None:
        config = UmlConfig.of(spec)
        if spec.boot is None or spec.boot.kernel is None:
            raise BackendError(f"{spec.name}: a UML guest needs vivarium.boot.kernel, the UML kernel it runs as")
        if spec.boot.firmware is not None:
            raise BackendError(f"{spec.name}: UML boots no firmware")
        root = spec.boot.root
        if root is not None and root.format is RootFormat.QCOW2 and config.qemu_img is None:
            raise BackendError(f"{spec.name}: a qcow2 root image needs qemuImg in vivarium.launch.config")

    @staticmethod
    def with_kernel(spec: MachineSpec, kernel: Path) -> MachineSpec:
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
        spec = machine.spec
        config = UmlConfig.of(spec)
        boot = boot or spec.boot
        if boot is None or boot.kernel is None:
            raise BackendError(f"{spec.name}: no vivarium.boot.kernel")
        ram = physmem_dir(spec.memory)
        uml_dir, cleanup = socket_dir(rundir, f"{mconsole.UMID}/mconsole", ram)
        kernel = [
            str(boot.kernel.image),
            *await self._root(config, rundir, spec, boot.root, boot.kernel, kept_store(machine, rundir)),
            "rw",
            f"mem={spec.memory}",
            f"ssl0=fd:{agent_fd}",
            # Catch the guest's syscalls with a seccomp filter instead of
            # ptrace: fewer context switches per trap and per page fault,
            # which measures a few percent on throughput and about five
            # seconds off a boot.  "auto" falls back to ptrace where the
            # host will not let us install a filter, rather than
            # refusing to boot the way "on" does.  `vivarium.seccomp` sets
            # it; "off" is the ptrace userspace -- see issue #8.
            f"seccomp={spec.seccomp}",
            # The management console, which is how the host asks this
            # guest to give memory back.  `uml_dir` defaults to ~/.uml,
            # and a build sandbox has no writable HOME; the run directory
            # is per guest, so one fixed umid in it collides with nothing.
            f"uml_dir={uml_dir}",
            f"umid={mconsole.UMID}",
        ]
        cpu_args, notes = processors(boot.kernel, spec.cpus)
        kernel += cpu_args
        if machine.artifacts is not None:
            # The kernel does not know this one, so it hands it to /init
            # as an environment variable -- "will be passed to user space"
            # in the boot log. modules/image.nix mounts it there, with
            # busybox, and says why it cannot be a mount unit.
            kernel.append(f"{ARTIFACTS_ENV}={machine.artifacts}")
        # What /init mounts as the guest's /nix; see modules/image.nix.
        kernel.append(f"{STORE_ENV}={spec.store}")

        helpers: list[subprocess.Popen[bytes]] = []
        if spec.vsock:
            # No vec0 from the host: the guest reaches it over vsock, a
            # vhost-user device the egress serves (E7). The kernel connects
            # at boot and fails if nothing listens yet.
            device = uml_dir / "vsock.sock"
            notes += forward.egress_notes(machine.spec.uplink_network, machine.offline)
            try:
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
                machine.egress_relay = f"vsock:{RELAY_IN_PORT}"
            except BaseException:
                abandon(helpers, ())
                raise
            kernel.append(f"virtio_uml.device={device}:{VIRTIO_ID_VSOCK}")
            argv = kernel
        elif spec.uplink is None:
            raise BackendError(f"{spec.name}: nothing serves vec0 under UML: uplink.proxy is {spec.uplink_proxy}")
        else:
            # vec0 joins a segment: the router's port for this guest, or an
            # out-of-band management network (nixidae/nixidae#958). A plain
            # fd transport like the segment NICs, and the guest leases its
            # address there.
            argv = [*kernel, self._vec(0, lan_fds["vec0"], spec.mtu, spec.mac(0))]
        for nic in spec.interfaces:
            argv.append(self._vec(nic.nic, lan_fds[nic.name], nic.mtu, nic.mac))

        pass_fds = (agent_fd, *lan_fds.values())
        env = dict(os.environ)
        # The guest's RAM, and nothing else: the runner's own temporary
        # files -- the cow file above among them -- keep the caller's
        # TMPDIR, because a guest's disk belongs on a disk.  See
        # PHYSMEM_DIRS.
        if ram is not None:
            env["TMPDIR"] = ram
        return Launch(
            argv=argv,
            pass_fds=pass_fds,
            env=env,
            controls=Uml._controls(mconsole.socket_path(uml_dir)),
            pid_file=uml_dir / mconsole.UMID / "pid",
            cleanup=cleanup,
            helpers=helpers,
            notes=notes,
        )

    @staticmethod
    def _controls(path: Path) -> Controls:
        """The management console at *path*, as a balloon.

        The reply comes back with `sendto` to the address the request came
        from, so the client needs a bound path of its own -- beside the
        guest's socket, which is the one directory already known to fit in
        a `sockaddr_un`.
        """
        console = mconsole.Mconsole(path, path.with_name("client"))
        return Controls(channel=console, balloon=console)

    @staticmethod
    async def _root(
        config: UmlConfig, rundir: Path, spec: MachineSpec, root: Root | None, kernel: Kernel, kept: Path | None
    ) -> list[str]:
        """The disks, the root and the kernel's parameters.

        A qcow2 image (a cloud image) is a format ubd cannot read. It
        becomes a raw file in the run directory, sparse, and the guest
        writes to it directly: it is this run's copy already, so it needs no
        cow. A squashfs (VyOS) is read-only, and its initrd overlays it.
        """
        if root is None:
            # vivarium's NixOS image: its /init mounts the store, then runs
            # the system's init. See modules/image.nix.
            # The kept store is /dev/ubdb, as modules/kept.nix names it.
            kept_disk = [f"ubd1={kept}"] if kept is not None else []
            return [
                f"ubd0={rundir}/cow,{spec.image}",
                *kept_disk,
                "root=/dev/ubda",
                "init=/init",
                *kernel.cmdline.split(),
            ]
        match root.format:
            case RootFormat.SQUASHFS:
                disks = [f"ubd0r={spec.image}"]
            case RootFormat.QCOW2:
                if config.qemu_img is None:
                    raise BackendError(f"{spec.name}: a qcow2 root image needs qemuImg in vivarium.launch.config")
                raw = rundir / "disk.raw"
                await anyio.run_process([str(config.qemu_img), "convert", "-O", "raw", str(spec.image), str(raw)])
                disks = [f"ubd0={raw}"]
            case RootFormat.RAW:
                disks = [f"ubd0={rundir}/cow,{spec.image}"]
        if config.seed is not None:
            disks.append(f"ubd1r={config.seed}")
        if kernel.initrd is not None:
            disks.append(f"initrd={kernel.initrd}")
        else:
            disks.append(f"root=/dev/ubda{root.partition or ''}")
        return [*disks, *kernel.cmdline.split()]

    @staticmethod
    def _vec(unit: int, fd: int, mtu: int, mac: str | None = None) -> str:
        """A ``vecN=`` device on *fd*.

        ``mtu`` is only settable here: the driver leaves ``max_mtu`` at
        ``ether_setup``'s 1500, so ``ip link set mtu`` cannot raise it
        afterwards.

        No ``gro=1``: all it does is fix the receive buffers at 64K so
        that a transport with virtio-net headers can deliver a segment
        larger than the MTU.  ``fd`` has no such headers -- nothing ever
        arrives bigger than a frame -- so it would only mean allocating
        64K per frame and throwing most of it away.
        """
        address = f",mac={mac}" if mac else ""
        return f"vec{unit}:transport=fd,fd={fd},depth={_VECTOR_DEPTH},mtu={mtu}{address}"
