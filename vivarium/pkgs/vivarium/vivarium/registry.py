"""Which runs are up on this host, for `vivarium ps`.

A run with a control socket makes its runtime directory
(`places.run_directory`) as it starts: its sockets, and a link to its
`--out`. It removes the directory as it ends. A SIGKILLed run leaves it,
so a reader removes an entry whose run ended: `liveness`, the run's lock,
says so, not the entry or a socket that is slow to answer.

A runner older than `places` leaves a bare link to its `--out` here
instead, with its socket in `--out`; a reader takes both.
"""

from __future__ import annotations

import contextlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import anyio

from . import liveness, places
from .control import RUN_FILE, SOCKET, Op, Reply, RunFile, request

if TYPE_CHECKING:
    from collections.abc import Iterator

ASK: Final = 2.0
"""Seconds a run has to answer `state`; a live one that does not shows as
`UNANSWERED`."""


def directory() -> Path:
    return places.runtime() / places.RUNS


def pin() -> None:
    places.pin()


@contextlib.contextmanager
def registered(out: Path, where: Path | None = None) -> Iterator[Path]:
    """The run's runtime directory while the block runs; `where` is for a test."""
    entry = places.prepare(out) if where is None else _prepare_in(out, where)
    link = entry / places.OUT_LINK
    try:
        yield entry
    finally:
        # Only our own: a later run into the same `--out` may have taken it.
        with contextlib.suppress(OSError):
            if link.readlink() == out.resolve():
                shutil.rmtree(entry)


def _prepare_in(out: Path, where: Path) -> Path:
    entry = places.run_directory(out, places.private(where))
    entry.mkdir(mode=0o700, exist_ok=True)
    (entry / places.OUT_LINK).unlink(missing_ok=True)
    (entry / places.OUT_LINK).symlink_to(out.resolve())
    return entry


@dataclass(frozen=True, slots=True)
class Up:
    out: Path
    state: str
    """`ready`, `paused` or `running`, as `ctl state` answers."""
    name: str | None
    """The test's name, from `run.json`."""
    pause: str | None
    environment: str | None = None
    """The environment's name, for a run in `<state>/envs`."""


def _entry(entry: Path) -> tuple[Path, Path] | None:
    """`(out, socket)` of one entry, either layout."""
    try:
        if entry.is_symlink():
            out = entry.readlink()
            return out, out / SOCKET
        return (entry / places.OUT_LINK).readlink(), entry / SOCKET
    except OSError:
        return None


UNANSWERED: Final = "unanswered"
"""The state of a live run whose socket did not answer in `ASK` seconds:
not bound yet, or its loop is busy."""


async def _remove(entry: anyio.Path) -> None:
    if await entry.is_symlink():
        await entry.unlink(missing_ok=True)
    else:
        await anyio.to_thread.run_sync(lambda gone=Path(entry): shutil.rmtree(gone, ignore_errors=True))


async def runs(where: Path | None = None) -> list[Up]:
    """Every live run. An entry is removed only on proof that its run
    ended: its `run.lock` free, its `--out` gone, or for a runner without a
    lock, a socket that refuses."""
    found: list[Up] = []
    folder = anyio.Path(where or directory())
    if not await folder.is_dir():
        return found
    async for entry in folder.iterdir():
        located = _entry(Path(entry))
        # No link yet: a run between `mkdir` and `symlink` in `places.prepare`.
        if located is None:
            continue
        out = located[0]
        if not await anyio.Path(out).is_dir():
            await _remove(entry)
            continue
        with liveness.dead(out) as ended:
            if ended:
                await _remove(entry)
                continue
        legacy = liveness.life(out) is liveness.Life.ABSENT
        try:
            with anyio.fail_after(ASK):
                reply = await request(located[1], Op.STATE)
        except (OSError, TimeoutError) as error:
            if legacy:
                # Only a socket nobody listens on proves an old runner gone.
                if isinstance(error, ConnectionRefusedError):
                    await _remove(entry)
                continue
            reply = Reply(ok=True, result=UNANSWERED)
        name = None
        with contextlib.suppress(OSError, ValueError, KeyError):
            about = RunFile.from_json(await anyio.Path(out / RUN_FILE).read_text())
            name = None if about.test is None else about.test.name
        found.append(
            Up(
                out=out,
                state=reply.result or "",
                name=name,
                pause=reply.pause,
                environment=places.environment_name(out),
            )
        )
    return sorted(found, key=lambda up: str(up.out))
