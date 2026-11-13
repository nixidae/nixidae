"""Which runs are up on this host, for `vivarium ls`.

A run with a control socket puts a link to its `--out` here as it starts
and removes it as it ends. A SIGKILLed run leaves its link, so a reader
asks each run's socket and removes a link whose socket does not answer:
the socket, not the link, says a run is up.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import anyio

from .control import RUN_FILE, SOCKET, Op, RunFile, request

if TYPE_CHECKING:
    from collections.abc import Iterator

ASK: Final = 2.0
"""Seconds a run has to answer `state` before it counts as gone."""


def directory() -> Path:
    """`$XDG_RUNTIME_DIR/vivarium/runs`, else one per user under /tmp."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) / "vivarium" if runtime else Path("/tmp") / f"vivarium-{os.getuid()}"  # noqa: S108 -- per user, made 0700
    return base / "runs"


def entry(out: Path, where: Path | None = None) -> Path:
    """The link of the run in `out`: named by the path, so a second run
    into the same `--out` takes the first one's place."""
    return (where or directory()) / hashlib.sha256(str(out).encode()).hexdigest()[:16]


@contextlib.contextmanager
def registered(out: Path, where: Path | None = None) -> Iterator[None]:
    link = entry(out, where)
    link.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    link.symlink_to(out)
    try:
        yield
    finally:
        # Only our own: a later run into the same `--out` may have replaced it.
        with contextlib.suppress(OSError):
            if link.readlink() == out:
                link.unlink()


@dataclass(frozen=True, slots=True)
class Up:
    out: Path
    state: str
    """`ready`, `paused` or `running`, as `ctl state` answers."""
    name: str | None
    """The test's name, from `run.json`."""
    pause: str | None


async def runs(where: Path | None = None) -> list[Up]:
    """Every run whose socket answers; a link to one that does not is removed."""
    found: list[Up] = []
    folder = anyio.Path(where or directory())
    if not await folder.is_dir():
        return found
    async for link in folder.iterdir():
        out = Path(await link.readlink())
        try:
            with anyio.fail_after(ASK):
                reply = await request(out / SOCKET, Op.STATE)
        except (OSError, TimeoutError):
            await link.unlink(missing_ok=True)
            continue
        name = None
        with contextlib.suppress(OSError, ValueError, KeyError):
            about = RunFile.from_json(await anyio.Path(out / RUN_FILE).read_text())
            name = None if about.test is None else about.test.name
        found.append(Up(out=out, state=reply.result or "", name=name, pause=reply.pause))
    return sorted(found, key=lambda up: str(up.out))
