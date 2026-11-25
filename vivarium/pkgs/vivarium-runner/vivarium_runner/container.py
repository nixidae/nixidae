"""A guest as a rootless OCI container, run by crun.

No kernel boot: the guest's systemd runs as PID 1 in a user, PID, mount,
cgroup and network namespace of its own, as the user who started the run.
``docs/design/history/running-anywhere.md`` (Area 8) has the measurements this is
built on.

Three pieces, each for a fact measured before it was written:

* :func:`oci_config`, the bundle's ``config.json``. Pure, so it is tested
  without a container.
* :func:`probe`, what the host must have, tried rather than read.
* :func:`main`, the launcher. crun gives systemd a console only with
  ``terminal: true``, and then hands the pty master over a socket instead
  of relaying it -- to a pipe it says "tcgetattr: Inappropriate ioctl for
  device" and exits. The launcher takes the master and copies it to its own
  stdout, which is what :class:`~vivarium_runner.machine.Machine` reads.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import fcntl
import functools
import json
import os
import pwd
import selectors
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import TypedDict, cast

import anyio

from . import storeview
from .lifetime import die_with_parent

AGENT_DIR = "/run/host/agent"
"""Where the guest sees the host's agent directory."""
KEPT_ROOT = "/.vivarium-kept"
"""`vivarium.persist`'s store in the guest: `root` in modules/kept.nix."""

AGENT_SOCKET = f"{AGENT_DIR}/sock"

SUPERVISOR = "supervisor"
"""The leaf the launcher, crun and the network helpers move to, in a cgroup of their own (:func:`_split_cgroup`)."""

GUEST_CGROUP = "guest"

SUBORDINATE_IDS = 65536
"""How many ids beyond root the guest gets. 65534 is ``nobody``, so fewer
leaves a service that runs as it unable to start."""

CAPABILITIES = [
    "CAP_" + name
    for name in [
        "CHOWN",
        "DAC_OVERRIDE",
        "DAC_READ_SEARCH",
        "FOWNER",
        "FSETID",
        "KILL",
        "SETGID",
        "SETUID",
        "SETPCAP",
        "LINUX_IMMUTABLE",
        "NET_BIND_SERVICE",
        "NET_BROADCAST",
        "NET_ADMIN",
        "NET_RAW",
        "IPC_LOCK",
        "IPC_OWNER",
        "SYS_MODULE",
        "SYS_RAWIO",
        "SYS_CHROOT",
        "SYS_PTRACE",
        "SYS_PACCT",
        "SYS_ADMIN",
        "SYS_BOOT",
        "SYS_NICE",
        "SYS_RESOURCE",
        "SYS_TIME",
        "SYS_TTY_CONFIG",
        "MKNOD",
        "LEASE",
        "AUDIT_WRITE",
        "AUDIT_CONTROL",
        "SETFCAP",
        "MAC_OVERRIDE",
        "MAC_ADMIN",
        "SYSLOG",
        "WAKE_ALARM",
        "BLOCK_SUSPEND",
        "AUDIT_READ",
        "PERFMON",
        "BPF",
        "CHECKPOINT_RESTORE",
    ]
]
"""Every capability, in the guest's own user namespace only. An empty set
is what crun gives when the field is left out, and systemd then fails
every ``User=`` service at the GROUP step (measured)."""


@dataclass(frozen=True)
class Range:
    """A run of subordinate ids from ``/etc/subuid`` or ``/etc/subgid``."""

    start: int
    count: int


def subordinate(path: Path, user: str, uid: int) -> Range | None:
    """The first range *path* gives *user*, by name or by number."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.strip().split(":")
        if len(fields) == 3 and fields[0] in (user, str(uid)):
            return Range(int(fields[1]), int(fields[2]))
    return None


class IdMap(TypedDict):
    """One line of an OCI ``uidMappings`` or ``gidMappings``."""

    containerID: int
    hostID: int
    size: int


class Mount(TypedDict):
    """One entry of an OCI ``mounts``."""

    destination: str
    type: str
    source: str
    options: list[str]


class CrunState(TypedDict):
    """The part of ``crun state`` output that is read."""

    pid: int


def _mapping(host: int, extra: Range) -> list[IdMap]:
    return [
        {"containerID": 0, "hostID": host, "size": 1},
        {"containerID": 1, "hostID": extra.start, "size": SUBORDINATE_IDS},
    ]


def _bind(source: str, destination: str, *options: str) -> Mount:
    return {
        "destination": destination,
        "type": "bind",
        "source": source,
        "options": ["rbind", *options],
    }


def _fs(kind: str, destination: str, *options: str) -> Mount:
    return {
        "destination": destination,
        "type": kind,
        "source": kind,
        "options": list(options),
    }


def oci_config(
    *,
    hostname: str,
    init: str,
    setpriv: str,
    rootfs: Path,
    store: str,
    agent_dir: Path,
    artifacts: Path | None,
    uid: int,
    gid: int,
    subuid: Range | None,
    subgid: Range | None,
    writable_store: bool = True,
    store_view: bool = False,
    kvm: bool = False,
    kept: Path | None = None,
) -> dict[str, object]:
    # The OCI runtime spec is an open JSON document; crun reads its keys, not this code.
    """The ``config.json`` for one guest.

    The mounts are in the order crun applies them, and the order matters:
    the agent's bind lands on the ``/run`` tmpfs, so it comes after it.

    With no subordinate ids the guest shares the runner's user namespace.
    That is the `uid-range` sandbox, where the build is already root with
    65536 ids, as nixpkgs' nspawn tests run with ``--private-users=no``.

    Without ``writable_store``, ``/nix/store`` is the host's, bound
    read-only and recursively: the sandbox's store is one bind mount per
    input, and an overlay does not see a lower's submounts.

    With ``store_view``, *store* is the guest's writable store view
    (``storeview.build``), bound in as it is: it holds its own binds.

    With ``kvm``, the host's ``/dev/kvm`` is bound in (``nestedVirtualization``).
    """
    mounts = [
        _fs("proc", "/proc", "nosuid", "noexec", "nodev"),
        _fs("tmpfs", "/dev", "nosuid", "strictatime", "mode=755", "size=65536k"),
        # gid 3 is NixOS' `tty`. NixOS' own devpts mount asks for it, and
        # says "Invalid gid '3'" where the guest has only one id.
        _fs(
            "devpts",
            "/dev/pts",
            "nosuid",
            "noexec",
            "newinstance",
            "ptmxmode=0666",
            "mode=0620",
            "gid=3",
        ),
        _fs("tmpfs", "/dev/shm", "nosuid", "noexec", "nodev", "mode=1777"),
        _fs("mqueue", "/dev/mqueue", "nosuid", "noexec", "nodev"),
        # Read-only, which is what keeps udevd from starting: its unit has
        # `ConditionPathIsReadWrite=/sys`. A udevd in a user namespace gets
        # no uevents, so every link stayed "pending" and networkd never
        # configured vec0 (measured).
        _fs("sysfs", "/sys", "nosuid", "noexec", "nodev", "ro"),
        # `cgroup2`, a plain mount, and not OCI's `cgroup`: crun reads what
        # that means from the host's /sys/fs/cgroup, which a uid-range
        # build does not mount, and says "invalid file system type".
        _fs("cgroup2", "/sys/fs/cgroup", "nosuid", "noexec", "nodev", "rw"),
        _fs("tmpfs", "/run", "nosuid", "nodev", "mode=755"),
        # No mount at /tmp: the rootfs's own directory, on the run root's
        # disk, which the run's cleaner removes however the run ends. A
        # tmpfs puts every store and build the pynixd suites make in host
        # memory (nixidae/nixidae#1258). A directory cannot be unlinked
        # from the host early: a removed directory refuses new entries.
        # The host's store below, the guest's writes above. `userxattr`
        # because an unprivileged overlay may not use trusted.* xattrs.
        #
        # /nix/store and not all of /nix, as the other backends have it.
        # Unprivileged, `lowerdir=/nix` fails with EINVAL where the host's
        # /nix/store is a mount of its own, and `lowerdir=/nix/store`
        # works (measured with unshare). One mount for all of /nix is for
        # a kubelet `subPath`, and a container guest runs no kubelet.
        {
            "destination": "/nix/store",
            "type": "overlay",
            "source": "overlay",
            "options": [
                f"lowerdir={store}/store",
                f"upperdir={rootfs}/.nix-upper",
                f"workdir={rootfs}/.nix-work",
                "userxattr",
            ],
        }
        if writable_store and not store_view
        else _bind(f"{store}/store", "/nix/store", "rw" if store_view else "ro"),
        _bind(str(agent_dir), AGENT_DIR, "rw"),
    ]
    if artifacts is not None:
        mounts.append(_bind(str(artifacts), "/artifacts", "rw"))
    if kept is not None:
        # modules/kept.nix's root; a container has no disk to format.
        mounts.append(_bind(str(kept), KEPT_ROOT, "rw"))
    if kvm:
        mounts.append(_bind("/dev/kvm", "/dev/kvm", "rw"))

    return {
        "ociVersion": "1.0.2",
        "hostname": hostname,
        "root": {"path": str(rootfs), "readonly": False},
        "process": {
            "terminal": True,
            "user": {"uid": 0, "gid": 0},
            # The init dies with crun, and the PID namespace with it. crun
            # does not arrange that: SIGKILLed, it left the guest running.
            "args": [setpriv, "--pdeathsig", "KILL", "--", init],
            "env": [
                "PATH=/run/current-system/sw/bin",
                "container=crun",
                "TERM=dumb",
            ],
            "cwd": "/",
            "capabilities": {
                "bounding": CAPABILITIES,
                "effective": CAPABILITIES,
                "permitted": CAPABILITIES,
            },
            "noNewPrivileges": False,
        },
        "mounts": mounts,
        "linux": {
            "namespaces": [{"type": kind} for kind in ("pid", "ipc", "uts", "mount", "cgroup", "network")]
            + ([{"type": "user"}] if subuid is not None else []),
            "maskedPaths": [],
            "readonlyPaths": [],
        }
        | (
            {"uidMappings": _mapping(uid, subuid), "gidMappings": _mapping(gid, subgid)}
            if subuid is not None and subgid is not None
            else {}
        ),
    }


# ── what the host must have ─────────────────────────────────────────


@dataclass(frozen=True)
class Missing:
    """One thing the host lacks, and what gives it."""

    what: str
    why: str
    remedy: str

    def __str__(self) -> str:
        return f"{self.what}: {self.why}\n    fix: {self.remedy}"


_USERNS_PROBE = "import os; os.unshare(os.CLONE_NEWUSER)"
"""A child that makes a user namespace of its own. In the child's own code
and not a `preexec_fn`: `anyio.run_process` takes none."""

_USERNS_HOLD = "import os, sys; os.unshare(os.CLONE_NEWUSER); print(flush=True); sys.stdin.read()"
"""`_USERNS_PROBE`, then a line once the namespace exists, then alive until stdin closes."""


class Host:
    """The probes of this host, each run once and then remembered.

    A probe is a process or two, and a run asked them again for every
    guest: 140-175 ms of each container launch (measured). What the host
    can do does not change while a run goes on.

    Guests launch at once, so each answer has a lock: the first launch
    probes, and the others wait for its answer. One lock for each answer,
    because `probe` asks for `link_failure` and `scope` while it holds
    `missing`'s, and an anyio lock is not reentrant.
    """

    def __init__(self) -> None:
        self._missing: dict[bool, list[Missing]] = {}
        self._link_failure: list[str | None] = []
        self._scopes: dict[int | None, tuple[str, ...] | None] = {}
        self._missing_lock = anyio.Lock()
        self._link_lock = anyio.Lock()
        self._scope_lock = anyio.Lock()

    async def missing(self, *, lan: bool) -> list[Missing]:
        """`probe` of this host."""
        async with self._missing_lock:
            if lan not in self._missing:
                self._missing[lan] = await probe(lan=lan, host=self)
        return list(self._missing[lan])

    async def link_failure(self) -> str | None:
        """`link_fails` of this host."""
        async with self._link_lock:
            if not self._link_failure:
                self._link_failure.append(await link_fails())
        return self._link_failure[0]

    async def scope(self, memory_max: int | None = None) -> list[str] | None:
        """`scope` of this host, by *memory_max*."""
        async with self._scope_lock:
            if memory_max not in self._scopes:
                found = await scope(memory_max)
                self._scopes[memory_max] = None if found is None else tuple(found)
        prefix = self._scopes[memory_max]
        return None if prefix is None else list(prefix)


async def probe(
    user: str | None = None, uid: int | None = None, *, lan: bool = False, host: Host | None = None
) -> list[Missing]:
    """What this host lacks for a container guest; empty when nothing.

    Each check does the thing once. Reading configuration is not enough:
    an AppArmor profile or a seccomp filter shows only as a failed attempt.

    ``lan`` adds what a LAN needs: a veth pair moved between network
    namespaces. ``host`` answers the probes a launch asks again.
    """
    uid = os.getuid() if uid is None else uid
    user = user or pwd.getpwuid(uid).pw_name
    host = Host() if host is None else host
    missing: list[Missing] = []

    if lan:
        why = await host.link_failure()
        if why:
            missing.append(Missing("a veth pair", why, "load the veth kernel module on the host (modprobe veth)"))

    userns = (await anyio.run_process([sys.executable, "-c", _USERNS_PROBE], check=False)).returncode == 0
    if not userns:
        missing.append(
            Missing(
                "a user namespace",
                "unshare(CLONE_NEWUSER) failed",
                "allow unprivileged user namespaces: user.max_user_namespaces > 0, "
                "and on Ubuntu kernel.apparmor_restrict_unprivileged_userns=0",
            )
        )

    # In the uid-range sandbox the runner is root already, with the ids a
    # guest needs, and there is no /etc/subuid or newuidmap to ask.
    if not owns_ids():
        await _probe_ranges(user, uid, userns, missing)

    if not await cgroup_works() and await host.scope() is None:
        missing.append(
            Missing(
                "a cgroup systemd can write in",
                "a cgroup2 mounted in a new cgroup namespace was not writable, "
                "and systemd-run --user could not make a delegated scope",
                "run under a user systemd, or in a cgroup delegated to you; in "
                "a Nix build, ask for the uid-range system feature",
            )
        )
    return missing


def owns_ids() -> bool:
    """Root in a user namespace that maps at least a guest's worth of ids:
    a `uid-range` build."""
    if os.getuid() != 0:
        return False
    try:
        lines = Path("/proc/self/uid_map").read_text().splitlines()
    except OSError:
        return False
    return sum(int(line.split()[2]) for line in lines) >= SUBORDINATE_IDS


NO_SETUID = "no-setuid"
"""A file in the agent directory when setuid bits cannot be set: a Nix
build's seccomp filter refuses them, for the container too ("chmod: ...
Operation not permitted" from suid-sgid-wrappers, measured).
modules/container.nix skips that unit on it rather than failing the boot.
A file and not PID 1's environment: NixOS' stage 2 starts systemd without
the variable, and a `ConditionEnvironment` on it never held (measured)."""


def setuid_allowed(directory: Path) -> bool:
    """Whether a file in *directory* can be made setuid. Tried, not read."""
    probe_file = directory / "setuid-probe"
    probe_file.write_bytes(b"")
    try:
        probe_file.chmod(0o4755)
        return bool(probe_file.stat().st_mode & 0o4000)
    except PermissionError:
        return False
    finally:
        probe_file.unlink()


def own_cgroup_memory_kib(pid: int) -> int | None:
    """``memory.current`` of *pid*'s cgroup, when that cgroup is its own.

    ``None`` when *pid* shares this process's cgroup, which then holds
    more than the guest, or when the cgroup has no memory controller: a
    ``uid-range`` build's cgroup has none, and cannot add one (measured).
    """

    def path(of: str) -> str:
        return Path(f"/proc/{of}/cgroup").read_text().strip().split(":", 2)[2]

    try:
        mine = path(str(pid))
        if mine == path("self"):
            return None
        # The launcher's leaf: the guest is its sibling, and the parent counts both.
        mine = mine.removesuffix(f"/{SUPERVISOR}")
        return int(Path(f"/sys/fs/cgroup{mine}/memory.current").read_text()) // 1024
    except (OSError, ValueError, IndexError):
        return None


def tree_pss_kib(root: int) -> int:
    """Kibibytes the host pays for *root* and every process under it.

    A container has no memory file of its own the way a UML or QEMU guest
    has: its memory is its processes'. PSS and not RSS, so a page the
    processes share with each other or with the host's page cache counts
    once, split between its users. Raises when no process's figure could
    be read, rather than report a guest that costs nothing.
    """
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            ppid = int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(entry.name))

    total, read, todo = 0, 0, [root]
    while todo:
        pid = todo.pop()
        todo += children.get(pid, [])
        try:
            rollup = Path(f"/proc/{pid}/smaps_rollup").read_text()
        except OSError:
            continue
        pss = next((line.split()[1] for line in rollup.splitlines() if line.startswith("Pss:")), None)
        if pss is not None:
            total += int(pss)
            read += 1
    if read == 0:
        raise RuntimeError(f"no process under {root} has a readable smaps_rollup")
    return total


def store_is_one_mount(store: str) -> bool:
    """Whether *store*/store has no mounts under it. A sandbox's store has
    one bind per input, which an overlay lower does not show."""
    prefix = f"{store}/store/"
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return False
    return not any(line.split()[4].startswith(prefix) for line in lines)


_CGROUP_PROBE = """
import ctypes, os, sys, tempfile
uid, gid = os.getuid(), os.getgid()
os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS | os.CLONE_NEWCGROUP)
open("/proc/self/setgroups", "w").write("deny")
open("/proc/self/uid_map", "w").write(f"0 {uid} 1")
open("/proc/self/gid_map", "w").write(f"0 {gid} 1")
target = tempfile.mkdtemp()
libc = ctypes.CDLL(None, use_errno=True)
if libc.mount(b"none", target.encode(), b"cgroup2", 0, None) != 0:
    sys.exit("mount: " + os.strerror(ctypes.get_errno()))
os.mkdir(target + "/probe")
os.rmdir(target + "/probe")
"""
"""What systemd in the guest will do: mount a cgroup2 in its own cgroup
namespace and make a group in it. Not a check of the runner's cgroup
directory: a `uid-range` build has no /sys/fs/cgroup mounted at all, and
this still works there (measured)."""


_LINK_PROBE = """
import os
from vivarium_runner.container import _open_link
uid, gid = os.getuid(), os.getgid()
os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNET)
open("/proc/self/setgroups", "w").write("deny")
open("/proc/self/uid_map", "w").write(f"0 {uid} 1")
open("/proc/self/gid_map", "w").write(f"0 {gid} 1")
_open_link(os.open("/proc/self/ns/net", os.O_RDONLY), "probe0", 1500, "52:54:00:00:00:01").close()
"""
"""What the LAN relay does, with a namespace of its own standing in for
the guest's: a veth pair, one end moved, an AF_PACKET socket on the other.
A host kernel without veth, or a seccomp filter, shows only here."""


async def link_fails() -> str | None:
    """Why no LAN link can be made here, or ``None`` when one can."""
    done = await anyio.run_process([sys.executable, "-c", _LINK_PROBE], check=False)
    if done.returncode == 0:
        return None
    return (done.stderr.decode(errors="replace").strip().splitlines() or ["failed"])[-1]


async def cgroup_works(prefix: list[str] | None = None) -> bool:
    done = await anyio.run_process([*(prefix or []), sys.executable, "-c", _CGROUP_PROBE], check=False)
    return done.returncode == 0


async def _probe_ranges(user: str, uid: int, userns: bool, missing: list[Missing]) -> None:
    ranges = {}
    for name, path in (("subuid", Path("/etc/subuid")), ("subgid", Path("/etc/subgid"))):
        found = subordinate(path, user, uid)
        if found is None or found.count < SUBORDINATE_IDS:
            missing.append(
                Missing(
                    f"{SUBORDINATE_IDS} subordinate ids in {path}",
                    f"{user} has {found.count if found else 'none'}",
                    f"NixOS: users.users.{user}.autoSubUidGidRange = true; "
                    f"elsewhere: usermod --add-{name}s 100000-165535 {user}",
                )
            )
        else:
            ranges[name] = found
    if userns and len(ranges) == 2:
        for helper, host, extra in (
            ("newuidmap", uid, ranges["subuid"]),
            ("newgidmap", os.getgid(), ranges["subgid"]),
        ):
            why = await _try_map(helper, host, extra)
            if why:
                missing.append(
                    Missing(
                        f"a working {helper}",
                        why,
                        "install shadow's uidmap tools, setuid or with "
                        "cap_setuid/cap_setgid; NixOS has them in /run/wrappers/bin",
                    )
                )


SCOPE = ["--user", "--scope", "--quiet", "--collect", "-p", "Delegate=yes"]


async def scope(memory_max: int | None = None) -> list[str] | None:
    """A command prefix that runs its command in a delegated cgroup, or
    ``None`` when the user's systemd will not make one.

    The scope is the guest's own cgroup: ``memory.current`` there is what
    the host pays for it, and *memory_max* bytes is its limit.

    ``systemd-run --scope`` execs the command in its own process rather
    than forking it, so the parent-death signal the runner set survives.
    The host's systemd-run, not one from the store: it talks to the host's
    user manager, and the two must agree.
    """
    path = shutil.which("systemd-run")
    if path is None:
        return None
    prefix = [path, *SCOPE, *(["-p", f"MemoryMax={memory_max}"] if memory_max else [])]
    return prefix if await cgroup_works(prefix) else None


async def _try_map(helper: str, host: int, extra: Range) -> str | None:
    """Map one subordinate id into a fresh user namespace with *helper*.

    Tried, not read: NixOS' wrappers carry file capabilities and no setuid
    bit, so a mode check refuses a helper that works (measured).
    """
    path = shutil.which(helper)
    if path is None:
        return "not on PATH"
    async with await anyio.open_process([sys.executable, "-c", _USERNS_HOLD]) as child:
        if child.stdin is None or child.stdout is None:
            raise RuntimeError("open_process gave no pipes")
        try:
            await child.stdout.receive()
        except anyio.EndOfStream:
            return "a child could not make a user namespace"
        done = await anyio.run_process(
            [path, str(child.pid), "0", str(host), "1", "1", str(extra.start), "1"], check=False
        )
        await child.stdin.aclose()
    if done.returncode != 0:
        said = (done.stderr or done.stdout).decode(errors="replace").strip()
        return f"{path} failed: {said}"
    return None


# ── the launcher ─────────────────────────────────────────────────────


def _relay(master: int, crun: subprocess.Popen[bytes]) -> None:
    """Copy the guest's console to stdout until the guest is gone."""
    out = sys.stdout.buffer
    selector = selectors.DefaultSelector()
    selector.register(master, selectors.EVENT_READ)
    while True:
        if not selector.select(timeout=0.5):
            if crun.poll() is not None:
                return
            continue
        try:
            data = os.read(master, 65536)
        except OSError:
            # EIO: no slave end is open at this moment. That is not the
            # end of the guest: systemd hangs the console up as it starts
            # and opens it again, and a relay that stopped here lost every
            # line after "starting systemd..." (measured). crun's exit is
            # the end.
            if crun.poll() is not None:
                return
            time.sleep(0.05)
            continue
        if not data:
            return
        out.write(data)
        out.flush()


def _shares_userns(pid: int) -> bool:
    """Whether *pid* is in this process's user namespace. Joining the one
    you are in is EINVAL."""
    return Path(f"/proc/{pid}/ns/user").readlink() == Path("/proc/self/ns/user").readlink()


_SYS_OPEN_TREE = 428
_SYS_MOVE_MOUNT = 429
_OPEN_TREE_CLONE = 1
_MOVE_MOUNT_F_EMPTY_PATH = 4

INIT_PID = "init.pid"
"""In crun's state directory: the guest's init, for `attach`."""


def attach(pid: int, paths: list[str]) -> int:
    """Mount store paths at their own place in *pid*'s mount namespace.

    Each is cloned here with `open_tree`, read-only, while the host's
    store is reachable; then this process joins the guest's namespaces
    and moves each clone onto the mount point `storeview.add` made. A
    process of its own: joining a mount namespace needs a single thread.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    trees: list[tuple[str, int]] = []
    for path in paths:
        fd = libc.syscall(_SYS_OPEN_TREE, -100, path.encode(), _OPEN_TREE_CLONE | os.O_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), f"open_tree {path}")
        storeview.read_only_tree(fd, path)
        trees.append((path, fd))
    joins = [("mnt", os.CLONE_NEWNS)]
    if not _shares_userns(pid):
        joins.insert(0, ("user", os.CLONE_NEWUSER))
    for kind, flag in joins:
        ns = os.open(f"/proc/{pid}/ns/{kind}", os.O_RDONLY)
        os.setns(ns, flag)
        os.close(ns)
    for path, fd in trees:
        if libc.syscall(_SYS_MOVE_MOUNT, fd, b"", -100, path.encode(), _MOVE_MOUNT_F_EMPTY_PATH) != 0:
            raise OSError(ctypes.get_errno(), f"move_mount {path}")
        os.close(fd)
    return 0


STATE_WAIT = 10.0
"""Seconds for crun to record a container it has already sent a pty for."""


def _init_pid(crun: list[str], name: str, proc: subprocess.Popen[bytes]) -> int:
    """The container's init, once crun has written down that it exists.

    The two come from different processes in crun 1.29: the container's
    init sends the pty (`container_init_setup`, `src/libcrun/container.c:1241`)
    and the parent writes the state afterwards (`container.c:2807`). Asked
    the moment the pty arrived, `state` failed for one of five containers
    started together, and the guest was reported as never coming up.
    """
    deadline = time.monotonic() + STATE_WAIT
    while True:
        done = subprocess.run([*crun, "state", name], capture_output=True, text=True)
        if done.returncode == 0:
            # crun writes this JSON, so its shape is crun's, not the parser's.
            return cast(CrunState, json.loads(done.stdout))["pid"]
        if proc.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError(f"crun state {name} failed ({done.returncode}): {done.stderr.strip()}")
        time.sleep(0.02)


NLMSG_ERROR = 2
RTM_NEWLINK = 16
RTM_SETLINK = 19
NLM_F_REQUEST = 0x1
NLM_F_ACK = 0x4
NLM_F_EXCL = 0x200
NLM_F_CREATE = 0x400
IFLA_ADDRESS = 1
IFLA_IFNAME = 3
IFLA_MTU = 4
IFLA_LINKINFO = 18
IFLA_NET_NS_FD = 28
IFLA_INFO_KIND = 1
IFLA_INFO_DATA = 2
VETH_INFO_PEER = 1
SIOCGIFINDEX = 0x8933
SIOCGIFFLAGS = 0x8913
SIOCSIFFLAGS = 0x8914
SIOCETHTOOL = 0x8946
IFF_UP = 0x1
ETHTOOL_GFEATURES = 0x3A
ETHTOOL_SFEATURES = 0x3B
ETH_P_ALL = 0x0003
SOL_PACKET = 263
PACKET_IGNORE_OUTGOING = 23
RELAY_ENDS = ("relay0", "relay1")
"""Names for the relay's end of the pair, in the relay's own network
namespace. Both ends exist there before the move, so the relay takes the
first one that is not the guest's name."""


def _rtattr(kind: int, payload: bytes) -> bytes:
    raw = struct.pack("HH", 4 + len(payload), kind) + payload
    return raw + b"\0" * (-len(raw) % 4)


def _ifinfomsg(index: int = 0) -> bytes:
    return struct.pack("BxHiII", socket.AF_UNSPEC, 0, index, 0, 0)


def _rtnetlink(kind: int, body: bytes, flags: int = 0) -> None:
    """Send one rtnetlink request and raise its error, if the kernel answers one."""
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as nl:
        nl.bind((0, 0))
        nl.send(struct.pack("IHHII", 16 + len(body), kind, NLM_F_REQUEST | NLM_F_ACK | flags, 1, 0) + body)
        reply = nl.recv(65536)
    if struct.unpack_from("H", reply, 4)[0] == NLMSG_ERROR:
        error = -struct.unpack_from("i", reply, 16)[0]
        if error:
            raise OSError(error, f"rtnetlink {kind}: {os.strerror(error)}")


def _ethtool(control: socket.socket, name: str, request: bytes) -> bytes:
    data = ctypes.create_string_buffer(request, len(request))
    fcntl.ioctl(control, SIOCETHTOOL, struct.pack("16sP8x", name.encode(), ctypes.addressof(data)))
    return data.raw


def _features_off(control: socket.socket, name: str) -> None:
    """Turn off every feature of *name* that can change.

    Not the per-feature calls (ETHTOOL_STSO and the like): each covers a
    fixed set, and a newer kernel's bits stay on. Measured on 7.2:
    tx-tcp-accecn, tx-udp_tnl and tx-gre segmentation stayed on.
    """
    words = 8
    got = _ethtool(control, name, struct.pack("II", ETHTOOL_GFEATURES, words) + bytes(16 * words))
    words = struct.unpack_from("I", got, 4)[0]
    available = [struct.unpack_from("I", got, 8 + 16 * word)[0] for word in range(words)]
    blocks = b"".join(struct.pack("II", valid, 0) for valid in available)
    _ethtool(control, name, struct.pack("II", ETHTOOL_SFEATURES, words) + blocks)


def _up(control: socket.socket, name: str) -> None:
    request = struct.pack("16s16x", name.encode())
    flags = struct.unpack_from("H", fcntl.ioctl(control, SIOCGIFFLAGS, request), 16)[0]
    fcntl.ioctl(control, SIOCSIFFLAGS, struct.pack("16sH14x", name.encode(), flags | IFF_UP))


def _open_link(guest_net: int, name: str, mtu: int, mac: str) -> socket.socket:
    """Give the network namespace *guest_net* the interface *name*; return the far end.

    A veth pair, made in a network namespace of the caller's own, which
    must be root in the user namespace that owns *guest_net*. The far end
    is an AF_PACKET socket: one frame per call, as a tap was, and no
    /dev/net/tun, which a Nix sandbox lacks.

    Traps, each measured:
    - veth offloads checksums and segmentation. A frame read off the
      relay's end carries a partial checksum, the peer drops it, and TCP
      never connects; a GSO frame is larger than the MTU. So every
      feature of *name* goes off before it reaches the guest.
    - The relay's end is a host of its own to IPv6: it would answer the
      guest's all-nodes ping and solicit routers. IPv6 is off in the
      relay's namespace.
    - AF_PACKET hands a socket the frames it sent itself, too:
      PACKET_IGNORE_OUTGOING.
    """
    relay_end = next(end for end in RELAY_ENDS if end != name)
    os.unshare(os.CLONE_NEWNET)
    for scope in ("all", "default"):
        Path(f"/proc/sys/net/ipv6/conf/{scope}/disable_ipv6").write_text("1")
    mtu_attr = _rtattr(IFLA_MTU, struct.pack("I", mtu))
    peer = _ifinfomsg() + _rtattr(IFLA_IFNAME, name.encode() + b"\0") + mtu_attr
    peer += _rtattr(IFLA_ADDRESS, bytes.fromhex(mac.replace(":", "")))
    kind = _rtattr(IFLA_INFO_KIND, b"veth") + _rtattr(IFLA_INFO_DATA, _rtattr(VETH_INFO_PEER, peer))
    link = _ifinfomsg() + _rtattr(IFLA_IFNAME, relay_end.encode() + b"\0") + mtu_attr + _rtattr(IFLA_LINKINFO, kind)
    _rtnetlink(RTM_NEWLINK, link, NLM_F_CREATE | NLM_F_EXCL)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        _features_off(control, name)
        index = struct.unpack_from("i", fcntl.ioctl(control, SIOCGIFINDEX, struct.pack("16s16x", name.encode())), 16)[0]
        _up(control, relay_end)
    _rtnetlink(RTM_SETLINK, _ifinfomsg(index) + _rtattr(IFLA_NET_NS_FD, struct.pack("I", guest_net)))
    end = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    end.setsockopt(SOL_PACKET, PACKET_IGNORE_OUTGOING, 1)
    end.bind((relay_end, ETH_P_ALL))
    return end


def link_relay(pid: int, lan: int, name: str, mtu: int, mac: str) -> int:
    """Be the guest's ``vec1``: a veth into its namespace, one frame each way.

    A segment fd carries one raw frame per datagram, which is what UML's
    ``transport=fd`` and QEMU's ``dgram`` take; an AF_PACKET socket reads
    and writes one frame per call. So the relay is a copy, and one segment
    holds guests of all three kinds.

    Joins the guest's user namespace first, which makes this root there:
    moving a link into the guest's network namespace needs CAP_NET_ADMIN
    in the namespace that owns it. A process must be single-threaded to
    join a user namespace, which is why this is a process of its own.
    """
    guest_net = os.open(f"/proc/{pid}/ns/net", os.O_RDONLY)
    if not _shares_userns(pid):
        user = os.open(f"/proc/{pid}/ns/user", os.O_RDONLY)
        os.setns(user, os.CLONE_NEWUSER)
        os.close(user)
    end = _open_link(guest_net, name, mtu, mac)
    os.close(guest_net)
    selector = selectors.DefaultSelector()
    selector.register(end, selectors.EVENT_READ, lan)
    selector.register(lan, selectors.EVENT_READ, end.fileno())
    while True:
        for key, _ in selector.select():
            try:
                frame = os.read(key.fd, 65536)
            except BlockingIOError:
                continue
            except OSError:
                return 0
            if not frame:
                return 0
            # A full queue drops the frame, as a wire would.
            with contextlib.suppress(OSError):
                os.write(key.data, frame)


def _lan(pid: int, fd: int, name: str, mtu: int, mac: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "vivarium_runner.crun_launch",
            "link",
            "--pid",
            str(pid),
            "--fd",
            str(fd),
            "--name",
            name,
            "--mtu",
            str(mtu),
            "--mac",
            mac,
        ],
        pass_fds=(fd,),
        start_new_session=True,
        preexec_fn=die_with_parent,
    )


MS_REC = 0x4000
MS_PRIVATE = 1 << 18


def _cgroup2_mounted() -> bool:
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, _, after = line.partition(" - ")
        if before.split()[4] == "/sys/fs/cgroup" and after.split()[0] == "cgroup2":
            return True
    return False


def _ensure_cgroup2() -> None:
    """Give crun the cgroup2 at /sys/fs/cgroup it insists on.

    crun statfs()es /sys/fs/cgroup before anything else and refuses what
    is not cgroup2 or tmpfs: "invalid file system type". A uid-range build
    mounts nothing there. As root in its own user namespace, the launcher
    can make a mount and cgroup namespace of its own and mount one: the
    build's cgroup, which Nix delegated to it.
    """
    if _cgroup2_mounted() or os.getuid() != 0:
        return
    os.unshare(os.CLONE_NEWNS | os.CLONE_NEWCGROUP)
    libc = ctypes.CDLL(None, use_errno=True)
    for args in (
        (b"none", b"/", None, MS_REC | MS_PRIVATE, None),
        (b"none", b"/sys/fs/cgroup", b"cgroup2", 0, None),
    ):
        if libc.mount(*args) != 0:
            raise OSError(ctypes.get_errno(), f"mount {args[1].decode()}")


def _split_cgroup(bundle: Path, name: str) -> bool:
    """Give the guest a cgroup of its own below the launcher's.

    cgroup v2 enables no controller for the children of a cgroup that holds a process. Without
    this, crun puts the guest's init beside the launcher, and the guest's cgroup root holds host
    processes it sees as pid 0: no controller reaches the guest. So, when the launcher's cgroup
    holds it alone, the launcher and every helper it starts move to `SUPERVISOR`, and crun's
    cgroupfs manager makes `GUEST_CGROUP` and enables the controllers (`enable_controllers`,
    `cgroup-utils.c`).

    A uid-range build's cgroup holds the build and every guest of the run, and has no controller
    to enable, so a process beside a child is no conflict. Each guest still gets a child, named for
    it: in the build's cgroup itself, two guests' systemds manage one tree, and one prunes a unit's
    cgroup while the other spawns into it (`Failed to spawn executor: No such file or directory`,
    nixidae/nixidae#1621).
    """
    try:
        own = Path("/proc/self/cgroup").read_text().strip().split(":", 2)[2]
        base = Path("/sys/fs/cgroup") / own.lstrip("/")
        if not os.access(base, os.W_OK):
            return False
        alone = (base / "cgroup.procs").read_text().split() == [str(os.getpid())]
        if alone and os.access(base / "cgroup.subtree_control", os.W_OK):
            (base / SUPERVISOR).mkdir(exist_ok=True)
            (base / SUPERVISOR / "cgroup.procs").write_text(str(os.getpid()))
            leaf = GUEST_CGROUP
        elif (base / "cgroup.controllers").read_text().split():
            return False
        else:
            leaf = f"{GUEST_CGROUP}-{name}"
    except (OSError, IndexError):
        return False
    config = json.loads((bundle / "config.json").read_text())
    config["linux"]["cgroupsPath"] = f"{own.rstrip('/')}/{leaf}"
    (bundle / "config.json").write_text(json.dumps(config))
    return True


def _report(lan: bool) -> int:
    """The probe as a program: what a derivation runs to fail early."""
    missing = anyio.run(functools.partial(probe, lan=lan))
    for item in missing:
        print(f"missing {item}", flush=True)  # noqa: T201 -- the probe's program output
    if missing:
        print(f"this host cannot run a container guest: {len(missing)} missing", flush=True)  # noqa: T201 -- the probe's program output
        return 1
    ids = "the build's own (uid-range)" if owns_ids() else "subordinate ids"
    store = "overlay" if store_is_one_mount("/nix") else "read-only bind"
    uplink = "yes" if anyio.run(link_fails) is None else "no (no veth pair)"
    print(f"ok: user namespace, cgroup, ids: {ids}, store: {store}, uplink: {uplink}")  # noqa: T201 -- the probe's program output
    return 0


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="vivarium_runner.crun_launch")
    sub = parser.add_subparsers(dest="mode", required=True)
    run = sub.add_parser("run")
    run.add_argument("--crun", required=True)
    run.add_argument("--state", required=True)
    run.add_argument("--bundle", required=True)
    run.add_argument("--name", required=True)
    run.add_argument(
        "--lan",
        action="append",
        default=[],
        help="FD,NAME,MAC,MTU: one segment interface; repeat for each",
    )
    check = sub.add_parser("probe", help="say what this host lacks, and exit 1 if anything")
    check.add_argument("--lan", action="store_true", help="a LAN will be asked for")
    bind = sub.add_parser("attach", help="mount store paths inside a running guest")
    bind.add_argument("--pid", type=int, required=True)
    bind.add_argument("paths", nargs="+")
    link = sub.add_parser("link")
    link.add_argument("--pid", type=int, required=True)
    link.add_argument("--fd", type=int, required=True)
    link.add_argument("--name", required=True)
    link.add_argument("--mtu", type=int, required=True)
    link.add_argument("--mac", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """``python -m vivarium_runner.crun_launch run|link ...``.

    ``run`` starts the guest and relays its console, and with ``--lan``
    a segment interface for each. ``link`` is the relay of one of them.
    """
    args = _parse(sys.argv[1:] if argv is None else argv)
    if args.mode == "link":
        return link_relay(args.pid, args.fd, args.name, args.mtu, args.mac)
    if args.mode == "probe":
        return _report(args.lan)
    if args.mode == "attach":
        return attach(args.pid, args.paths)
    _ensure_cgroup2()

    name = args.name
    bundle = args.bundle
    manager = "cgroupfs" if _split_cgroup(Path(bundle), name) else "disabled"
    crun = [args.crun, "--root", args.state, f"--cgroup-manager={manager}"]

    # A short directory: a sockaddr_un holds 108 bytes, and a bundle under
    # a long TMPDIR does not fit.
    sockets = Path(tempfile.mkdtemp(prefix="vivarium-crun-", dir=os.environ.get("VIVARIUM_RUN_ROOT") or "/tmp"))
    console = sockets / "console"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(console))
    listener.listen(1)

    # --no-new-keyring: a session keyring per container is charged to
    # the user every container maps root to, whose quota is 200 keys
    # (kernel.keys.maxkeys). The 21st of 30 containers failed with "join
    # keyctl: Disk quota exceeded" (nixidae/nixidae#903).
    proc = subprocess.Popen(
        [*crun, "run", "--no-new-keyring", "--bundle", bundle, "--console-socket", str(console), name],
        stdin=subprocess.DEVNULL,
        # Its own group, so a signal to the runner's group reaches this
        # process and not crun: crun would pass SIGTERM to systemd, which
        # reads it as "re-execute".
        start_new_session=True,
        preexec_fn=die_with_parent,
    )

    def stop(signum: int, _frame: FrameType | None) -> None:
        subprocess.run([*crun, "kill", name, "KILL"], capture_output=True)

    signal.signal(signal.SIGTERM, stop)

    listener.settimeout(0.5)
    master: int | None = None
    helpers: list[subprocess.Popen[bytes]] = []
    try:
        while master is None and proc.poll() is None:
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            _, fds, _, _ = socket.recv_fds(conn, 1024, 1)
            conn.close()
            if not fds:
                # crun connected and failed before it had a pty to send.
                # Its own error is already on stdout; its exit is the
                # status.
                continue
            master = fds[0]
        if master is not None:
            try:
                pid = _init_pid(crun, name, proc)
                (Path(args.state) / INIT_PID).write_text(f"{pid}\n")
                for lan in args.lan:
                    fd, nic, mac, mtu = lan.split(",")
                    helpers.append(_lan(pid, int(fd), nic, int(mtu), mac))
            except (RuntimeError, subprocess.CalledProcessError) as error:
                # On the console, which is where Machine looks for why a
                # guest did not come up.
                print(f"vivarium-crun: {error}", flush=True)  # noqa: T201 -- the launcher's stdout is the guest console
                return 1
        if master is not None:
            _relay(master, proc)
        return proc.wait()
    finally:
        listener.close()
        console.unlink(missing_ok=True)
        sockets.rmdir()
        if proc.poll() is None:
            stop(signal.SIGTERM, None)
            proc.wait()
        for helper in helpers:
            if helper.poll() is None:
                helper.kill()
                helper.wait()
        subprocess.run([*crun, "delete", "--force", name], capture_output=True)
