"""Where vivarium keeps things on the host, from platformdirs.

    state    user_state_path("vivarium"): environments (`envs/<name>`),
             the lab CA (`ca`). Outlives a reboot.
    runtime  user_runtime_path("vivarium"): one directory per live run
             (`runs/<key>`), with its sockets and a link to its `--out`.
             Gone at logout or reboot, so nothing stale outlives the host.

macOS has no `XDG_RUNTIME_DIR`; platformdirs answers
`~/Library/Caches/TemporaryItems` there.

`pin` fixes both in the environment before the runner enters its user
namespace: uid 0 there would name another user's directories.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Final

import platformdirs

STATE_ENV: Final = "VIVARIUM_STATE"
RUNTIME_ENV: Final = "VIVARIUM_RUNTIME"
LEGACY_REGISTRY_ENV: Final = "VIVARIUM_REGISTRY"
"""What a runner older than `places` reads for its registry directory."""

ENVIRONMENTS: Final = "envs"
RUNS: Final = "runs"
OUT_LINK: Final = "out"
"""In a run's runtime directory: a link to its `--out`."""

NAME: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


class PlaceError(RuntimeError):
    pass


def state() -> Path:
    if pinned := os.environ.get(STATE_ENV):
        return Path(pinned)
    return platformdirs.user_state_path("vivarium")


def runtime() -> Path:
    if pinned := os.environ.get(RUNTIME_ENV):
        return Path(pinned)
    return platformdirs.user_runtime_path("vivarium")


def pin() -> None:
    """Fix both directories for this process and its children."""
    os.environ.setdefault(STATE_ENV, str(state()))
    os.environ.setdefault(RUNTIME_ENV, str(runtime()))
    os.environ.setdefault(LEGACY_REGISTRY_ENV, str(runtime() / RUNS))


def private(directory: Path) -> Path:
    """*directory*, made 0700 if absent, and refused unless this user owns
    it and nobody else may enter: sockets in it answer for the user's runs."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    found = directory.stat()
    if found.st_uid != os.getuid() or found.st_mode & 0o077:
        raise PlaceError(f"{directory} is not this user's alone (owner {found.st_uid}, mode {found.st_mode & 0o777:o})")
    return directory


def key(out: Path) -> str:
    """A run's key: its resolved `--out`, hashed short enough for a socket path."""
    return hashlib.sha256(str(out.resolve()).encode()).hexdigest()[:16]


def run_directory(out: Path, root: Path | None = None) -> Path:
    """The live run's runtime directory; not made here."""
    return (root or runtime() / RUNS) / key(out)


def prepare(out: Path) -> Path:
    """The run's runtime directory, made with its link to *out*: where its
    sockets go. Safe to call twice."""
    entry = run_directory(out, private(runtime() / RUNS))
    if entry.is_symlink():
        # An older runner's entry for the same `--out`, which is not live:
        # this run took its `--out`.
        entry.unlink()
    entry.mkdir(mode=0o700, exist_ok=True)
    link = entry / OUT_LINK
    if not link.is_symlink() or link.readlink() != out.resolve():
        link.unlink(missing_ok=True)
        link.symlink_to(out.resolve())
    return entry


def socket(out: Path, name: str) -> Path:
    """Where a client finds the socket *name* of the run in *out*: its
    runtime directory, or `--out` for a runner older than `places`."""
    entry = run_directory(out) / name
    legacy = out / name
    if not entry.exists() and legacy.exists():
        return legacy
    return entry


def check_name(name: str) -> str:
    if not NAME.fullmatch(name):
        raise PlaceError(
            f"{name!r} is no environment name: lowercase letters, digits, '.', '_' and '-', 64 at most,"
            " starting with a letter or digit"
        )
    return name


def environment(name: str) -> Path:
    """An environment's `--out`: `<state>/envs/<name>`."""
    return state() / ENVIRONMENTS / check_name(name)


def environment_name(out: Path) -> str | None:
    """The name of the environment *out* is, or None for a `--out` of one's own."""
    out = out.resolve()
    if out.parent == (state() / ENVIRONMENTS).resolve() and NAME.fullmatch(out.name):
        return out.name
    return None
