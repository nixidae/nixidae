"""Which runs are up on this host, for `vivarium ps`.

A run with a control socket makes its runtime directory
(`places.run_directory`) as it starts: its sockets, and a link to its
`--out`. It removes the directory as it ends. A SIGKILLed run leaves it,
so a reader asks each run's socket and removes an entry whose socket does
not answer: the socket, not the entry, says a run is up.

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

from . import places
from .control import RUN_FILE, SOCKET, Op, RunFile, request

if TYPE_CHECKING:
    from collections.abc import Iterator

ASK: Final = 2.0
"""Seconds a run has to answer `state` before it counts as gone."""


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


async def runs(where: Path | None = None) -> list[Up]:
    """Every run whose socket answers; an entry for one that does not is removed."""
    found: list[Up] = []
    folder = anyio.Path(where or directory())
    if not await folder.is_dir():
        return found
    async for entry in folder.iterdir():
        located = _entry(Path(entry))
        try:
            if located is None:
                raise OSError("no link to an output directory")
            with anyio.fail_after(ASK):
                reply = await request(located[1], Op.STATE)
        except (OSError, TimeoutError):
            if await entry.is_symlink():
                await entry.unlink(missing_ok=True)
            else:
                await anyio.to_thread.run_sync(lambda gone=Path(entry): shutil.rmtree(gone, ignore_errors=True))
            continue
        out = located[0]
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
