"""Whether a run in an `--out` is alive: `<out>/run.lock`, locked by the run.

The kernel drops a `flock` when the last descriptor on it closes, SIGKILL
included, so a held lock is a live run and a free one a run that ended. A
pid in `run.json` says less: the kernel reuses it, and a run that is
evaluating or whose loop is stalled answers no socket.

`vivarium run` takes the lock before it evaluates and hands it to the
runner across `execv` (`VIVARIUM_RUN_LOCK`); a runner started without it,
such as a check's driver, takes its own. The runner's children never get
it: it is inheritable only until the runner adopts it.

A runner older than this file takes no lock, so `Life.ABSENT` means "ask
the old evidence", never "dead".
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from enum import Enum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterator, MutableMapping
    from pathlib import Path

LOCK: Final = "run.lock"
ENV: Final = "VIVARIUM_RUN_LOCK"
"""The descriptor of the lock a `vivarium run` hands to the runner it execs."""


class Life(Enum):
    HELD = "held"
    FREE = "free"
    """The run that held it ended."""
    ABSENT = "absent"
    """No run took it yet, or an older runner: no answer."""


class RunLive(RuntimeError):
    """Another run holds the lock on this `--out`."""


def life(out: Path) -> Life:
    with _attempt(out) as got:
        return got


@contextlib.contextmanager
def dead(out: Path) -> Iterator[bool]:
    """True when the run in *out* has ended. The reader's shared lock stays
    for the block: the ended run's lock file does not turn up held while
    the reader removes what that run left."""
    with _attempt(out) as got:
        yield got is Life.FREE


@contextlib.contextmanager
def _attempt(out: Path) -> Iterator[Life]:
    try:
        fd = os.open(out / LOCK, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError:
        yield Life.ABSENT
        return
    try:
        # Shared: readers never see each other as a run, and the run's
        # exclusive lock still refuses them.
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            yield Life.HELD
            return
        yield Life.FREE
    finally:
        os.close(fd)


def hold(out: Path, environ: MutableMapping[str, str] = os.environ) -> int:
    """This process's lock on *out*, for as long as it lives: the one a
    `vivarium run` handed down, else a new one. Raises RunLive when another
    run holds it."""
    handed = _adopt(out, environ)
    if handed is not None:
        return handed
    out.mkdir(parents=True, exist_ok=True)
    if life(out) is Life.HELD:
        raise RunLive(f"another run is live in {out}")
    # Locked under another name, then renamed: a reader never sees the file
    # unlocked between its creation and the flock.
    partial = out / f".{LOCK}.{os.getpid()}"
    fd = os.open(partial, os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    partial.replace(out / LOCK)
    return fd


def hand_down(fd: int, environ: MutableMapping[str, str] = os.environ) -> None:
    """Keep *fd* across this process's `execv`: the runner adopts it."""
    os.set_inheritable(fd, True)
    environ[ENV] = str(fd)


def forget(out: Path) -> None:
    """Remove an ended run's lock file, so a new run reads `ABSENT` until it
    takes its own and not the old run's `FREE`."""
    with dead(out) as ended:
        if ended:
            (out / LOCK).unlink(missing_ok=True)


def _adopt(out: Path, environ: MutableMapping[str, str]) -> int | None:
    raw = environ.pop(ENV, None)
    if raw is None:
        return None
    try:
        fd = int(raw)
        mine = os.fstat(fd)
    except (ValueError, OSError):
        return None
    os.set_inheritable(fd, False)
    try:
        there = (out / LOCK).stat()
    except OSError:
        return None
    if (mine.st_dev, mine.st_ino) != (there.st_dev, there.st_ino):
        return None
    return fd
