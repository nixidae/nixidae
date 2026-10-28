"""What a guest is made of, for each kind of machine.

:class:`Machine` owns a guest's lifetime and its console, and everything it
does above this file is the same whichever backend it has.  A backend's
whole job is to answer one question: what do I exec, with which fds, and
what else has to be running alongside it.

The host-side plumbing is shared, not reimplemented.  Both backends take:

    the agent socketpair    a fd carrying arpyc, raw
    the segment fd          a fd from :mod:`vivarium_runner.net`, raw frames
    passt                   started from :mod:`vivarium_runner.forward`'s specs

UML takes the segment fd as ``vec1:transport=fd``; QEMU takes the same fd
as ``dgram,local.type=fd``.  Both are plain ``send``/``recv`` on the fd
with no framing, which is why one segment can hold guests of either kind.

The difference is passt.  UML's vector transport has no length prefix and
passt has one, so a UML guest runs behind ``uml-passt-bridge``, which
forks passt and translates.  QEMU speaks passt's protocol itself, so the
bridge is not in the picture and the runner starts passt directly.
"""

from __future__ import annotations

import itertools
import json
import mmap
import os
import pwd
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import container, forward, mconsole, qmp


class BackendError(Exception):
    """A guest could not be assembled: a helper that would not start."""


PR_SET_PDEATHSIG = 1
"""``prctl`` option number, which Python's ``signal`` module does not
carry.  From ``include/uapi/linux/prctl.h``; the number is ABI."""


def die_with_parent() -> None:
    """Ask the kernel to SIGKILL this child when the runner dies.

    Every other guarantee here is cleanup code, and cleanup code does not
    run when the runner is killed with SIGKILL, dies on a fault, or is
    stopped by a build that gave up.  A guest that outlives its runner is
    then a UML kernel or a QEMU spinning on a core with nothing left to
    report to -- measured, one such guest used a whole core for 31 hours.

    ``PR_SET_PDEATHSIG`` is the only thing that covers that case: the
    kernel sends the signal, so nothing has to be running to send it.

    Called after ``setsid``, which does not clear it.  The ``getppid``
    check closes the race where the runner dies between the fork and this
    call, which would otherwise leave the child holding a death signal
    that can no longer arrive.
    """
    import ctypes

    parent = os.getppid()
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
    if os.getppid() != parent:
        os._exit(1)


def _tail(path: Path, lines: int = 20) -> str:
    """The end of *path*, for a helper that has already exited.

    The end and not the start: passt names the port it could not bind
    once per port and says why it gave up on the last line.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError as error:
        return f"({path} unreadable: {error})"
    return "\n".join(text.splitlines()[-lines:]) or f"({path} is empty)"


ARTIFACTS_ENV = "VIVARIUM_ARTIFACTS"
"""What the UML guest's /init reads its host directory from: one
guest's subdirectory of the run's artifacts."""

RUN_ROOT_ENV = "VIVARIUM_RUN_ROOT"
"""A short directory `vivarium run` removes however it ends; see
`vivarium/runroot.py`."""


def short_tmp() -> str:
    """Where a socket path short enough for `sockaddr_un` goes: the run
    root when there is one, which is cleaned up; else /tmp."""
    return os.environ.get(RUN_ROOT_ENV) or "/tmp"


STORE_ENV = "VIVARIUM_STORE"
"""What the UML guest's /init mounts as its /nix: the guest's store view,
or the host's /nix when it has none."""

ARTIFACTS_TAG = "artifacts"
"""The virtiofs tag QEMU serves the same directory under.
``modules/qemu.nix`` mounts it by this name."""

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


def memory_bytes(memory: str) -> int:
    """``mem=`` as a number, the way UML's ``memparse`` reads it."""
    scale = {"k": 1024, "m": 1024**2, "g": 1024**3}
    if memory[-1:].lower() in scale:
        return int(memory[:-1]) * scale[memory[-1].lower()]
    return int(memory)


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


def socket_dir(rundir: Path, name: str, fallback: str | None) -> tuple[Path, list[Path]]:
    """A directory to put *name* in where the kernel will accept the path.

    *rundir* where it fits, and a directory of its own where it does not:
    a unix socket's path goes in a ``sockaddr_un``, which holds 108 bytes,
    and a caller whose TMPDIR is long -- an agent's scratch directory is
    often 90 characters on its own -- would otherwise lose the control
    channel with nothing but a log line about it.

    Returns the directory and whatever has to be removed afterwards.
    """
    if len(str(rundir / name).encode()) < mconsole.UNIX_PATH_MAX:
        return rundir, []
    made = Path(tempfile.mkdtemp(prefix="vivarium-", dir=fallback or short_tmp()))
    return made, [made]


@dataclass
class Launch:
    """One guest's process, and whatever has to outlive its start."""

    argv: list[str]
    pass_fds: tuple[int, ...]
    env: dict[str, str] = field(default_factory=dict)
    helpers: list[subprocess.Popen] = field(default_factory=list)
    """Side processes the guest needs -- virtiofsd, passt -- for the
    backends that do not hide them behind something else.  Killed when the
    guest is torn down, in this order."""
    memory: Path | None = None
    """Where this guest answers a request to change its memory: UML's
    management console, QEMU's monitor. Which of the two it is follows
    from the backend, and :class:`Machine` asks the backend rather than
    the path."""
    pid_file: Path | None = None
    """Where the guest wrote its own pid, for a backend whose process is
    not the one the runner spawned. UML forks the kernel from the passt
    bridge, so the runner's own pid is the bridge's."""
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


class Uml:
    """A guest as an ordinary Linux process.

    Asks nothing of the host: no ``/dev/kvm``, no root, no tap device. One
    processor, and a trap into the host kernel for every guest syscall.
    """

    name = "uml"

    def launch(self, machine, rundir: Path, agent_fd: int, lan_fds: dict[str, int]) -> Launch:
        spec, tools = machine.spec, machine.tools
        ram = physmem_dir(spec.memory)
        uml_dir, cleanup = socket_dir(rundir, f"{mconsole.UMID}/mconsole", ram)
        kernel = [
            str(tools.kernel),
            *self._root(tools, rundir, spec),
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
        if machine.artifacts is not None:
            # The kernel does not know this one, so it hands it to /init
            # as an environment variable -- "will be passed to user space"
            # in the boot log. modules/image.nix mounts it there, with
            # busybox, and says why it cannot be a mount unit.
            kernel.append(f"{ARTIFACTS_ENV}={machine.artifacts}")
        # What /init mounts as the guest's /nix; see modules/image.nix.
        kernel.append(f"{STORE_ENV}={spec.store}")

        if spec.uplink is None:
            # vec0 is passt, behind the bridge: it forks passt and
            # translates its length-prefixed frames to the fd transport.
            argv = [
                str(tools.bridge),
                "--vec",
                self._vec(0, 3, spec.mtu),
                # The bridge forks passt, so the uplink's addressing reaches
                # it one argument at a time rather than directly.
                *(arg for value in forward.uplink_args(machine.offline) for arg in ("--passt", value)),
                *forward.to_args(machine.forward),
                *kernel,
            ]
        else:
            # vec0 joins an out-of-band management segment instead of passt
            # (nixidae/nixidae#958). No passt means no bridge: vec0 is a
            # plain fd transport like the segment NICs, straight to the
            # kernel, and the guest leases its management address there.
            argv = [*kernel, self._vec(0, lan_fds["vec0"], spec.mtu, spec.mac(0))]
        for nic in spec.interfaces:
            argv.append(self._vec(nic.nic, lan_fds[nic.name], spec.mtu, nic.mac))

        pass_fds = (agent_fd, *lan_fds.values())
        # The bridge finds passt on PATH.
        env = dict(os.environ, PATH=f"{tools.passt.parent}:{os.environ['PATH']}")
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
            memory=mconsole.socket_path(uml_dir),
            pid_file=uml_dir / mconsole.UMID / "pid",
            cleanup=cleanup,
        )

    @staticmethod
    def memory_control(path: Path) -> mconsole.Mconsole:
        """The client for `Launch.memory` under this backend.

        The reply comes back with `sendto` to the address the request came
        from, so the client needs a bound path of its own -- beside the
        guest's socket, which is the one directory already known to fit in
        a `sockaddr_un`.
        """
        return mconsole.Mconsole(path, path.with_name("client"))

    @staticmethod
    def _root(tools, rundir: Path, spec) -> list[str]:
        """The disks and the root, for a NixOS image, a cloud image or VyOS.

        A cloud image is qcow2, which ubd cannot read. It becomes a raw
        file in the run directory, sparse, and the guest writes to it
        directly: it is this run's copy already, so it needs no cow.
        """
        image = spec.boot.get("image")
        if image is None:
            return [f"ubd0={rundir}/cow,{spec.image}", "root=/dev/ubda", "init=/init"]
        if "initrd" in image:
            # VyOS: a squashfs, read-only, which the initramfs overlays.
            return [f"ubd0r={spec.image}", f"initrd={image['initrd']}", *image["kernelParams"]]
        raw = rundir / "disk.raw"
        subprocess.run(
            [str(tools.qemu_img), "convert", "-O", "raw", str(spec.image), str(raw)],
            check=True,
            capture_output=True,
        )
        return [
            f"ubd0={raw}",
            # The seed, read-only: cloud-init finds it by its label.
            f"ubd1r={image['seed']}",
            f"root=/dev/ubda{image['partition']}",
            f"init={image['init']}",
            *image["kernelParams"],
        ]

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

    def launch(
        self,
        machine,
        rundir: Path,
        agent_fd: int,
        lan_fds: dict[str, int],
        boot: dict | None = None,
        disk_fds: tuple[int, ...] = (),
    ) -> Launch:
        disk_boot = "image" in machine.spec.boot
        # virtiofsd serves a file as the guest user that made it, so a
        # guest user outside the namespace's ids gets EINVAL on its first
        # write to /artifacts, minutes in. Fail here instead.
        if not disk_boot and not container.owns_ids():
            raise BackendError(
                f"{machine.spec.name}: a QEMU guest needs {container.SUBORDINATE_IDS} ids "
                "in the run's namespace, and it maps fewer\n"
                "    fix: give your user subordinate ids in /etc/subuid and /etc/subgid, "
                "with newuidmap and newgidmap installed; a Nix build asks for uid-range"
            )
        helpers: list[subprocess.Popen] = []
        opened: list[int] = []
        try:
            if disk_boot:
                return self._launch_disk(machine, rundir, agent_fd, lan_fds, helpers, opened)
            return self._launch(machine, rundir, agent_fd, lan_fds, helpers, opened, boot, disk_fds)
        except BaseException:
            # Nothing owns these until a Launch carries them back, so a
            # failure between the first one and the last would leave a
            # daemon running, or an unlinked disk image alive with no name
            # and no way to find it. A test that fails while booting is
            # exactly when that happens.
            for helper in helpers:
                if helper.poll() is None:
                    helper.kill()
                    helper.wait()
            for fd in opened:
                os.close(fd)
            raise

    def _uplink_netdev(self, machine, rundir, lan_fds, helpers, opened) -> tuple[str, int | None]:
        """The `vec0` QEMU netdev, and the passt fd to keep open (or None).

        passt by default. When the spec names an uplink segment, vec0 is a
        dgram on that segment's fd instead, so the uplink joins an
        out-of-band management network a peer serves -- there is no passt,
        and so no forwards (nixidae/nixidae#958).
        """
        spec, tools = machine.spec, machine.tools
        if spec.uplink is not None:
            return f"dgram,id=vec0,local.type=fd,local.str={lan_fds['vec0']}", None
        passt_fd, passt_proc = self._passt(tools, rundir, machine.forward, machine.offline)
        helpers.append(passt_proc)
        opened.append(passt_fd)
        return f"stream,id=vec0,addr.type=fd,addr.str={passt_fd}", passt_fd

    def _launch(
        self,
        machine,
        rundir: Path,
        agent_fd: int,
        lan_fds: dict[str, int],
        helpers: list[subprocess.Popen],
        opened: list[int],
        boot: dict | None = None,
        disk_fds: tuple[int, ...] = (),
    ) -> Launch:
        spec, tools = machine.spec, machine.tools

        # Every unix socket this guest needs, in one directory short
        # enough to name them: virtiofsd's, the artifacts one, and the
        # monitor. `vfs` and `art` are no longer than `qmp`, so one check
        # covers all three.
        sockets, cleanup = socket_dir(rundir, "qmp", None)
        # A guest that rebooted left its monitor's socket.
        (sockets / "qmp").unlink(missing_ok=True)

        vfs_fd, vfsd = self._virtiofsd(tools, sockets, "vfs", spec.store)
        helpers.append(vfsd)
        opened.append(vfs_fd)

        art_fd: int | None = None
        if machine.artifacts is not None:
            art_fd, artd = self._virtiofsd(tools, sockets, "art", str(machine.artifacts))
            helpers.append(artd)
            opened.append(art_fd)

        # A reboot keeps the disk the guest wrote, and the runner owns it.
        # QEMU's image lock is an OFD lock: it belongs to the open file
        # description, which outlives the QEMU that took it. So a reboot
        # opens the unlinked image afresh and lets the old descriptions go.
        boot = boot or spec.boot
        efi = boot.get("efi")
        if disk_fds:
            disk_fds = tuple(
                os.open(f"/proc/self/fd/{fd}", flags)
                for fd, flags in zip(disk_fds, itertools.cycle((os.O_RDWR, os.O_RDONLY)))
            )
            opened.extend(disk_fds)
        else:
            disk_fds = self._scratch_disk(tools, rundir, spec.image)
            opened.extend(disk_fds)
            if efi:
                esp_fds = self._scratch_disk(tools, rundir, Path(efi["esp"]), backing="qcow2")
                opened.extend(esp_fds)
                disk_fds += esp_fds

        uplink, passt_fd = self._uplink_netdev(machine, rundir, lan_fds, helpers, opened)

        argv = [
            str(tools.qemu),
            "-machine",
            "q35,accel=kvm,memory-backend=guest-memory",
            # Nested virtualization only when the guest asked: `-cpu host`
            # alone hands every guest the host's vmx or svm, and udev
            # then loads KVM in it. See `nestedVirtualization`.
            "-cpu",
            "host" if boot.get("nested") else "host,-vmx,-svm",
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
            # The root, as /dev/vda, by fd rather than by name -- see
            # _scratch_disk. Two fds in the set because QEMU opens an
            # image O_RDONLY to probe its format before reopening it
            # O_RDWR, and matches the set on the access mode.
            "-add-fd",
            f"fd={disk_fds[0]},set=1",
            "-add-fd",
            f"fd={disk_fds[1]},set=1",
            "-drive",
            "file=/dev/fdset/1,if=virtio,format=qcow2",
            # The console, read by Machine._pump_console.
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
            "virtserialport,chardev=agent,name=vivarium-agent" if boot.get("efi") else "virtconsole,chardev=agent",
            "-netdev",
            uplink,
            "-device",
            f"virtio-net-pci,netdev=vec0,mac={spec.mac(0)}",
            # The guest tells QEMU which pages it has freed and QEMU
            # madvises them out of the memfd above, so the host stops
            # paying for a page cache the guest has dropped. Without it a
            # guest drifts towards its whole `-m` and stays there.
            "-device",
            "virtio-balloon-pci,free-page-reporting=on",
            # And the monitor, which is how the host asks for a size
            # rather than waiting for the guest to volunteer one.
            "-qmp",
            f"unix:{sockets}/qmp,server=on,wait=off",
        ]
        # A firmware boot has no kernel: systemd-boot on the ESP picks one.
        if boot.get("kernel"):
            argv += [
                "-kernel",
                boot["kernel"],
                "-initrd",
                boot["initrd"],
                "-append",
                f"{boot['cmdline']} init={boot['toplevel']}/init",
            ]
        if efi:
            # The variables outlive each QEMU, as NVRAM outlives a reset:
            # bootctl's boot entry and systemd-boot's LoaderEntryDefault are
            # there.
            variables = rundir / "efivars.fd"
            if not variables.exists():
                shutil.copyfile(efi["variables"], variables)
                variables.chmod(0o600)
            argv += [
                "-drive",
                f"if=pflash,format=raw,unit=0,readonly=on,file={efi['firmware']}",
                "-drive",
                f"if=pflash,format=raw,unit=1,file={variables}",
                "-add-fd",
                f"fd={disk_fds[2]},set=2",
                "-add-fd",
                f"fd={disk_fds[3]},set=2",
                "-drive",
                "file=/dev/fdset/2,if=virtio,format=qcow2",
            ]
        if display := boot.get("display"):
            argv += [
                # `-display none` stays: screendump reads the device's
                # surface, not a window.
                "-device",
                f"virtio-gpu-pci,xres={display['width']},yres={display['height']}",
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
        if art_fd is not None:
            argv += [
                "-chardev",
                f"socket,id=artifacts,fd={art_fd}",
                "-device",
                f"vhost-user-fs-pci,chardev=artifacts,tag={ARTIFACTS_TAG}",
            ]
        for nic in spec.interfaces:
            argv += [
                "-netdev",
                f"dgram,id=nic{nic.nic},local.type=fd,local.str={lan_fds[nic.name]}",
                "-device",
                f"virtio-net-pci,netdev=nic{nic.nic},mac={nic.mac},host_mtu={spec.mtu}",
            ]

        pass_fds = tuple(
            fd for fd in (agent_fd, passt_fd, *lan_fds.values(), vfs_fd, art_fd, *disk_fds) if fd is not None
        )
        return Launch(
            argv=argv,
            pass_fds=pass_fds,
            helpers=helpers,
            memory=sockets / "qmp",
            cleanup=cleanup,
            disk_fds=disk_fds,
        )

    def _launch_disk(
        self,
        machine,
        rundir: Path,
        agent_fd: int,
        lan_fds: dict[str, int],
        helpers: list[subprocess.Popen],
        opened: list[int],
    ) -> Launch:
        """Another operating system from its own disk: its own bootloader
        and kernel, and none of the store, the agent or virtiofs.

        The serial port is the control channel, the end of the socketpair
        the agent would get: `control.ConsoleControl` drives it. The
        uplink is the first NIC, so it is the OS's first interface.
        """
        spec, tools = machine.spec, machine.tools
        image = spec.boot["image"]
        sockets, cleanup = socket_dir(rundir, "qmp", None)

        disk_fds = self._scratch_disk(tools, rundir, spec.image, image.get("format", "raw"))
        opened.extend(disk_fds)

        uplink, passt_fd = self._uplink_netdev(machine, rundir, lan_fds, helpers, opened)

        argv = [
            str(tools.qemu),
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
            # Pinned: a second disk otherwise competes for the boot.
            "-device",
            "virtio-blk-pci,drive=root,bootindex=0",
            "-chardev",
            f"socket,id=control,fd={agent_fd}",
            "-serial",
            "chardev:control",
            "-netdev",
            uplink,
            "-device",
            f"virtio-net-pci,netdev=vec0,mac={spec.mac(0)}",
            "-qmp",
            f"unix:{sockets}/qmp,server=on,wait=off",
        ]
        if config := image.get("configDisk"):
            # A copy, writable: RouterOS does not mount a read-only disk.
            disk = rundir / "config.img"
            shutil.copyfile(config, disk)
            disk.chmod(0o600)
            argv += ["-drive", f"file={disk},if=virtio,format=raw"]
        for nic in spec.interfaces:
            argv += [
                "-netdev",
                f"dgram,id=nic{nic.nic},local.type=fd,local.str={lan_fds[nic.name]}",
                "-device",
                f"virtio-net-pci,netdev=nic{nic.nic},mac={nic.mac},host_mtu={spec.mtu}",
            ]
        pass_fds = tuple(fd for fd in (agent_fd, passt_fd, *lan_fds.values(), *disk_fds) if fd is not None)
        return Launch(
            argv=argv,
            pass_fds=pass_fds,
            helpers=helpers,
            memory=sockets / "qmp",
            cleanup=cleanup,
        )

    @staticmethod
    def memory_control(path: Path) -> qmp.Qmp:
        """The client for `Launch.memory` under this backend."""
        return qmp.Qmp(path)

    @staticmethod
    def _scratch_disk(tools, rundir: Path, image: Path | None, backing: str = "raw") -> tuple[int, int]:
        """A writable layer over the read-only root image, with no name.

        The same shape as UML's ``ubd0=<cow>,<image>``: the image stays in
        the store, and everything the guest writes goes here instead.

        The file is unlinked before QEMU is started and handed over as open
        file descriptors.  So the only thing keeping those gigabytes alive
        is a process, and the kernel frees them the moment QEMU is gone --
        including when the runner is killed with a signal it cannot catch,
        which no amount of cleanup code covers.

        Two descriptors, read-write and read-only, because QEMU opens an
        image O_RDONLY to probe its format before reopening it O_RDWR, and
        picks from the fd set by access mode.  With one it says
        ``Failed to find file descriptor with matching flags=0x0``.
        """
        if image is None:
            raise BackendError("this guest has no root image")
        scratch = rundir / "root.qcow2"
        done = subprocess.run(
            [
                str(tools.qemu_img),
                "create",
                "-q",
                "-f",
                "qcow2",
                # Named, because qemu-img refuses to guess a backing
                # format and a guess would be silently wrong. Raw for a
                # NixOS root and CHR; qcow2 for an image that ships one,
                # such as SONiC's.
                "-F",
                backing,
                "-b",
                str(image),
                str(scratch),
            ],
            capture_output=True,
            text=True,
        )
        if done.returncode != 0:
            raise BackendError(f"could not make a scratch disk over {image}: {(done.stderr or done.stdout).strip()}")
        fds = (os.open(scratch, os.O_RDWR), os.open(scratch, os.O_RDONLY))
        scratch.unlink()
        return fds

    @staticmethod
    def _virtiofsd(tools, rundir: Path, socket_name: str, shared: str) -> tuple[int, subprocess.Popen]:
        """Serve a host directory, and leave no socket behind.

        *socket_name* keeps the store's and the artifacts' sockets apart
        under *rundir*.

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

        proc = subprocess.Popen(
            [
                str(tools.virtiofsd),
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
        listener.close()

        # There is no socket file to wait for any more, so the readiness
        # check is that virtiofsd is still alive a moment later. Without
        # it, a bad argument or an unreadable shared directory would show
        # up as a guest whose initrd cannot mount /nix, which names the
        # wrong thing.
        time.sleep(0.05)
        if proc.poll() is not None:
            client.close()
            raise BackendError(f"virtiofsd exited ({proc.returncode}) instead of serving {shared}")

        return client.detach(), proc

    @staticmethod
    def _passt(tools, rundir: Path, rules, offline: bool = False) -> tuple[int, subprocess.Popen]:
        """Start passt on one end of a socketpair; return QEMU's end.

        No bridge: passt frames with a 4-byte big-endian length prefix,
        which is QEMU's own socket protocol, so the two talk directly.
        The specifiers are the ones forward.py builds for UML, unchanged.

        **passt keeps a log and is checked for a pulse**, because it used
        to have neither.  It ran `--quiet` with both streams on
        /dev/null and nothing looked at its exit status, so a passt that
        refused to start -- which it does outright when a port it was
        asked for will not bind -- left a guest with no uplink and no
        word said.  What that looks like from inside is a guest whose
        vec0 has a link-local address and nothing else, two minutes
        later, reported as a name that would not resolve.  Measured on a
        GitHub runner; see the `[cp] addresses and routes` of
        user-mode-nixos run 34943651620.
        """
        qemu_end, passt_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        log = rundir / "passt.log"
        handle = log.open("wb")
        proc = subprocess.Popen(
            [
                str(tools.passt),
                "--foreground",
                "--fd",
                str(passt_end.fileno()),
                *forward.uplink_args(offline),
                *forward.to_args(rules),
            ],
            stdin=subprocess.DEVNULL,
            pass_fds=(passt_end.fileno(),),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=die_with_parent,
        )
        passt_end.close()
        handle.close()

        # The same readiness check as virtiofsd above, and for the same
        # reason: passt fails at startup or not at all.
        time.sleep(0.05)
        if proc.poll() is not None:
            qemu_end.close()
            raise BackendError(f"passt exited ({proc.returncode}) instead of serving the uplink:\n{_tail(log)}")
        # Kept open until the guest has spawned; Machine closes it after.
        return qemu_end.detach(), proc


class Appliance(Qemu):
    """A vendor's disk image under QEMU: its own kernel, no NixOS, no agent.

    For a guest nothing of ours can run inside, a router's or a switch's
    virtual edition. What it shares with a QEMU guest is the hardware: the
    per-run disk layer, the passt uplink as the first NIC unless
    `vivarium.uplink.enable` is off, and each segment as one more, in `interfaces`
    order. So the appliance names its ports as it would on a PCI bus.

    No agent means no `succeed`. The runner reads the console until the
    spec's `ready` pattern, and a test reaches the guest over the network:
    through a `vivarium.forward` on the uplink, or from a NixOS guest on a
    segment.
    """

    name = "appliance"
    reboots = False

    def launch(
        self,
        machine,
        rundir: Path,
        agent_fd: int,
        lan_fds: dict[str, int],
        boot: dict | None = None,
        disk_fds: tuple[int, ...] = (),
    ) -> Launch:
        helpers: list[subprocess.Popen] = []
        opened: list[int] = []
        try:
            return self._appliance(machine, rundir, lan_fds, helpers, opened)
        except BaseException:
            for helper in helpers:
                if helper.poll() is None:
                    helper.kill()
                    helper.wait()
            for fd in opened:
                os.close(fd)
            raise

    def _appliance(
        self,
        machine,
        rundir: Path,
        lan_fds: dict[str, int],
        helpers: list[subprocess.Popen],
        opened: list[int],
    ) -> Launch:
        spec, tools = machine.spec, machine.tools
        appliance = spec.boot["appliance"]

        disk_fds = self._scratch_disk(tools, rundir, spec.image, appliance["format"])
        opened.extend(disk_fds)

        uplink = appliance.get("uplink", True)
        passt_fds: tuple[int, ...] = ()
        if uplink:
            passt_fd, passt_proc = self._passt(tools, rundir, machine.forward, machine.offline)
            helpers.append(passt_proc)
            opened.append(passt_fd)
            passt_fds = (passt_fd,)

        argv = [
            str(tools.qemu),
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
            "file=/dev/fdset/1,if=virtio,format=qcow2",
            "-serial",
            "stdio",
        ]
        if uplink:
            argv += [
                "-netdev",
                f"stream,id=vec0,addr.type=fd,addr.str={passt_fds[0]}",
                "-device",
                f"virtio-net-pci,netdev=vec0,mac={spec.mac(0)}",
            ]
        if appliance.get("kernel"):
            argv += [
                "-kernel",
                appliance["kernel"],
                "-initrd",
                appliance["initrd"],
                "-append",
                appliance.get("cmdline", ""),
            ]
        for nic in spec.interfaces:
            argv += [
                "-netdev",
                f"dgram,id=nic{nic.nic},local.type=fd,local.str={lan_fds[nic.name]}",
                "-device",
                f"virtio-net-pci,netdev=nic{nic.nic},mac={nic.mac},host_mtu={spec.mtu}",
            ]
        return Launch(
            argv=argv,
            pass_fds=(*passt_fds, *lan_fds.values(), *disk_fds),
            helpers=helpers,
        )


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

    The kernel, initrd and disk are QEMU's, so is the passt uplink. The
    store is served from inside the guest's own process, with a DAX window,
    so the guest maps the host's page cache of it instead of reading it
    into its own RAM. No monitor: no screen, and no balloon to resize.
    """

    name = "krun"

    def launch(self, machine, rundir: Path, agent_fd: int, lan_fds: dict[str, int]) -> Launch:
        # libkrun's virtio-fs serves a file as the guest user that made it,
        # the same as virtiofsd: see Qemu.launch.
        if not container.owns_ids():
            raise BackendError(
                f"{machine.spec.name}: a krun guest needs {container.SUBORDINATE_IDS} ids "
                "in the run's namespace, and it maps fewer\n"
                "    fix: give your user subordinate ids in /etc/subuid and /etc/subgid, "
                "with newuidmap and newgidmap installed; a Nix build asks for uid-range"
            )
        spec, tools = machine.spec, machine.tools
        opened: list[int] = []
        helpers: list[subprocess.Popen] = []
        try:
            disk_rw, disk_ro = Qemu._scratch_disk(tools, rundir, spec.image)
            opened += [disk_rw, disk_ro]
            passt_fd, passt_proc = Qemu._passt(tools, rundir, machine.forward, machine.offline)
            helpers.append(passt_proc)
            opened.append(passt_fd)
        except BaseException:
            for helper in helpers:
                helper.kill()
                helper.wait()
            for fd in opened:
                os.close(fd)
            raise
        # libkrun opens the disk by path, and only the fd keeps the unlinked
        # file alive. It opens it read-write and needs no read-only twin.
        os.close(disk_ro)

        boot = spec.boot
        launch_spec = {
            "libkrun": str(tools.libkrun),
            "cpus": spec.cpus,
            "memory_mib": memory_bytes(spec.memory) >> 20,
            "vmlinux": boot["vmlinux"],
            "initrd": boot["initrd"],
            "cmdline": f"{boot['cmdline']} init={boot['toplevel']}/init",
            "nested": bool(boot.get("nested")),
            "disk": f"/proc/self/fd/{disk_rw}",
            "store": spec.store,
            "dax_window": DAX_WINDOW,
            "agent_fd": agent_fd,
            "uplink": {"fd": passt_fd, "mac": spec.mac(0)},
            "lan": [{"fd": lan_fds[nic.name], "mac": nic.mac} for nic in spec.interfaces],
            "artifacts": None if machine.artifacts is None else str(machine.artifacts),
        }
        return Launch(
            argv=[
                sys.executable,
                "-m",
                "vivarium_runner.krun_launch",
                json.dumps(launch_spec),
            ],
            pass_fds=(agent_fd, passt_fd, disk_rw, *lan_fds.values()),
            helpers=helpers,
        )


class Container:
    """A guest as a rootless container: no kernel boot, the host's own.

    Needs what :func:`vivarium_runner.container.probe` checks, and says which of
    it is missing before anything starts. The uplink and the forwards are
    pasta's, joined to the guest's namespaces by the launcher. Each segment
    interface is a tap relayed to its segment fd, so a segment mixes all
    three kinds.
    No memory control yet.
    """

    name = "container"

    def launch(self, machine, rundir: Path, agent_fd: int, lan_fds: dict[str, int]) -> Launch:
        spec, tools = machine.spec, machine.tools
        missing = container.probe(tun=bool(lan_fds))
        if missing:
            raise BackendError("this host cannot run a container guest:\n" + "\n".join(f"  {item}" for item in missing))
        if spec.image is None:
            raise BackendError(f"{spec.name}: this guest has no root template")

        rootfs = rundir / "root"
        shutil.copytree(spec.image, rootfs, symlinks=True)
        # The template is in the store, so every copy is read-only.
        for directory, _, _ in os.walk(rootfs):
            os.chmod(directory, 0o755)

        sockets, cleanup = socket_dir(rundir, "agent/sock", None)
        agent_dir = sockets / "agent"
        agent_dir.mkdir()
        if not container.setuid_allowed(rundir):
            (agent_dir / container.NO_SETUID).touch()

        # pasta's tap and the LAN's are made on /dev/net/tun, which a Nix
        # sandbox has only with /dev/net in `extra-sandbox-paths`. With no
        # tun the guest has no uplink; the sandbox has no network anyway.
        # The uplink is taken where a tap can be made and left out where it
        # cannot; the LAN is required, and probe() checked it above.
        tun = bool(lan_fds) or container.tap_fails() is None

        uid, gid = os.getuid(), os.getgid()
        if container.owns_ids():
            subuid = subgid = None
        else:
            user = pwd.getpwuid(uid).pw_name
            subuid = container.subordinate(Path("/etc/subuid"), user, uid)
            subgid = container.subordinate(Path("/etc/subgid"), user, uid)
            assert subuid is not None and subgid is not None, "probe() checked both"

        bundle = rundir / "bundle"
        bundle.mkdir()
        (bundle / "config.json").write_text(
            json.dumps(
                container.oci_config(
                    hostname=spec.name,
                    init=f"{spec.boot['toplevel']}/init",
                    setpriv=str(tools.setpriv),
                    rootfs=rootfs,
                    store=spec.store,
                    agent_dir=agent_dir,
                    artifacts=machine.artifacts,
                    uid=uid,
                    gid=gid,
                    subuid=subuid,
                    subgid=subgid,
                    writable_store=container.store_is_one_mount(spec.store),
                    store_view=spec.store_paths is not None,
                )
            )
        )
        state = rundir / "crun"
        state.mkdir()
        lan = ["--mtu", str(spec.mtu)]
        for nic in spec.interfaces:
            lan += ["--lan", f"{lan_fds[nic.name]},{nic.name},{nic.mac}"]
        # A delegated scope of its own where the user's systemd makes one:
        # a cgroup the guest's systemd writes in, its memory counted apart
        # from every other guest's, and `vivarium.memory` as its limit.
        # Otherwise the cgroup this runs in, which is every uid-range build.
        prefix = container.scope(memory_bytes(spec.memory)) or []
        return Launch(
            argv=[
                *prefix,
                sys.executable,
                "-m",
                "vivarium_runner.crun_launch",
                "run",
                "--crun",
                str(tools.crun),
                "--state",
                str(state),
                "--bundle",
                str(bundle),
                "--name",
                f"uml-{spec.name}-{os.getpid()}",
                *lan,
                # The uplink: pasta joins the guest's namespaces once crun
                # has an init, with passt's arguments and forwards.
                *(
                    [
                        "--pasta-log",
                        str(rundir / "passt.log"),
                        "--",
                        str(tools.passt.with_name("pasta")),
                        "--foreground",
                        "--ns-ifname",
                        "vec0",
                        *forward.uplink_args(machine.offline),
                        *self._pasta_forwards(forward.to_args(machine.forward)),
                    ]
                    if tun
                    else []
                ),
            ],
            pass_fds=tuple(lan_fds.values()),
            agent_path=agent_dir / "sock",
            cleanup=cleanup,
        )

    @staticmethod
    def _pasta_forwards(args: list[str]) -> list[str]:
        """*args*, plus what makes pasta forward only what passt would.

        pasta forwards more by default: UDP ports it scans for, and the
        guest's loopback to the host's. passt does neither, so a guest
        that could reach the host's loopback here could not under UML.
        """
        extra = ["--tcp-ns", "none", "--udp-ns", "none"]
        for flag in ("--tcp-ports", "--udp-ports"):
            if flag not in args:
                extra += [flag, "none"]
        return [*args, *extra]


BACKENDS = {backend.name: backend() for backend in (Uml, Qemu, Krun, Container, Appliance)}


def get(name: str):
    """The backend called *name*, as the spec's ``backend`` field names it."""
    try:
        return BACKENDS[name]
    except KeyError:
        raise ValueError(f"no backend {name!r}; have {', '.join(sorted(BACKENDS))}") from None
