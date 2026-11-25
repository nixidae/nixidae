"""A run's own directory, and the cleaner that removes it however the run
ends.

Everything a run writes outside `--out` goes under one run root: `uml
run` points `TMPDIR` at it, and the runner's short-path fallbacks for
sockets use it too (`VIVARIUM_RUN_ROOT`). A normal end removes it. So does a
SIGKILL of the runner: before it enters its namespace, the runner forks
a cleaner in a session of its own, which waits on the runner's pidfd and
then removes the root. A killed process group does not take the cleaner
with it. A cleaner killed too is the last gap, and the next run's reaper
closes it: it removes every run root whose runner and cleaner are both
gone.

Measured before this: a SIGKILLed runner left one directory per guest
and one for crun, and 158 had built up in /tmp. Its processes and its
store views were already gone, the processes by parent-death signals and
the views with the mount namespace.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import os
import select
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

from vivarium_runner.machine import KEEP as KEEP

ENV = "VIVARIUM_RUN_ROOT"
PREFIX = "vivarium-run-"
OWNERS = "owners"
"""`<runner pid> <start> <cleaner pid> <start>`: who may still use a root.
A start time as well as a pid, so a reused pid is not taken for the run."""

REMOVE = "import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)"
"""Run as root of a user namespace that maps the run's ids: a container
guest's files belong to subordinate ids, which the caller alone may not
delete."""


def base() -> Path:
    """/tmp when writable, for short socket paths; else TMPDIR."""
    tmp = Path("/tmp")
    if tmp.is_dir() and os.access(tmp, os.W_OK):
        return tmp
    return Path(tempfile.gettempdir())


_PROC_PIDTBSDINFO = 3
_BSDINFO_SIZE = 136
_BSDINFO_START = 120
"""`struct proc_bsdinfo` (<sys/proc_info.h>): its size, and the offset of
`pbi_start_tvsec`."""


def _darwin_start_time(pid: int) -> int | None:
    """The process's start in seconds since the epoch, from libproc."""
    libc = ctypes.CDLL(ctypes.util.find_library("c"))
    info = ctypes.create_string_buffer(_BSDINFO_SIZE)
    if libc.proc_pidinfo(pid, _PROC_PIDTBSDINFO, ctypes.c_uint64(0), info, _BSDINFO_SIZE) != _BSDINFO_SIZE:
        return None
    return int.from_bytes(info.raw[_BSDINFO_START : _BSDINFO_START + 8], sys.byteorder)


def start_time(pid: int) -> int | None:
    """The process's start in clock ticks since boot (seconds since the
    epoch on macOS), or None if it is gone."""
    if sys.platform == "darwin":
        return _darwin_start_time(pid)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # Field 22; the name before it is in parentheses and may hold spaces.
    return int(stat.rsplit(")", 1)[1].split()[19])


def owners(runner: int, cleaner: int) -> str:
    return f"{runner} {start_time(runner)} {cleaner} {start_time(cleaner)}\n"


def stale(text: str, started: dict[int, int | None]) -> bool:
    """Whether no owner named in *text* is still the same process.

    *started* maps a pid to its start time now, None when it is gone.
    """
    fields = text.split()
    pairs = zip(fields[0::2], fields[1::2], strict=True)
    return not any(start != "None" and started.get(int(pid)) == int(start) for pid, start in pairs)


def reap(where: Path, remove_argv: list[str]) -> list[Path]:
    """Remove run roots whose runner and cleaner are both gone."""
    removed = []
    for root in where.glob(f"{PREFIX}*"):
        try:
            text = (root / OWNERS).read_text()
        except OSError:
            # No owners yet: a run between creating its root and writing
            # the file. Never someone else's to take.
            continue
        pids = [int(pid) for pid in text.split()[0::2]]
        if stale(text, {pid: start_time(pid) for pid in pids}):
            os.spawnv(os.P_WAIT, remove_argv[0], [*remove_argv, str(root)])
            removed.append(root)
    return removed


def remove_own() -> None:
    """The runner's own removal at its end. Inside the namespace, as root,
    so every file a guest left is ours to delete."""
    root = os.environ.get(ENV)
    if root and not os.environ.get(KEEP):
        shutil.rmtree(root, ignore_errors=True)


@contextlib.contextmanager
def exit_watch(pid: int) -> Iterator[int]:
    """A descriptor that turns readable when *pid* exits: a pidfd on
    Linux, a kqueue on macOS. ProcessLookupError when it is gone."""
    if sys.platform == "linux":
        pidfd = os.pidfd_open(pid)
        try:
            yield pidfd
        finally:
            os.close(pidfd)
    else:
        with contextlib.closing(select.kqueue()) as queue:
            queue.control([select.kevent(pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD, select.KQ_NOTE_EXIT)], 0)
            yield queue.fileno()


def spawn_cleaner(root: Path, remove_argv: list[str]) -> int:
    """Fork the cleaner; return its pid. The caller then execs the runner,
    which keeps the caller's pid: that is what the cleaner watches."""
    runner = os.getpid()
    child = os.fork()
    if child:
        return child
    try:
        os.setsid()
        # A pidfd on Linux; macOS has none, and a kqueue tells a process's exit.
        if sys.platform == "linux":
            watch = os.pidfd_open(runner)
        else:
            queue = select.kqueue()
            queue.control([select.kevent(runner, select.KQ_FILTER_PROC, select.KQ_EV_ADD, select.KQ_NOTE_EXIT)], 0)
            watch = queue.fileno()
        # Nothing the runner holds stays open here: a pipe from its
        # stdout would give its reader no EOF until the cleaner exits.
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        os.closerange(3, watch)
        os.closerange(watch + 1, os.sysconf("SC_OPEN_MAX"))
        # The watch proves the runner was alive when it was taken only if
        # the parent is still the runner afterwards.
        if os.getppid() == runner:
            if sys.platform == "linux":
                select.select([watch], [], [])
            else:
                queue.control(None, 1)
        if not os.environ.get(KEEP) and root.exists():
            os.execv(remove_argv[0], [*remove_argv, str(root)])
    finally:
        os._exit(0)
