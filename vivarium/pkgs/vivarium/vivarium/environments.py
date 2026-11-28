"""Named environments on this host: `<state>/envs/<name>`, up or not.

A directory is an environment's whole record: its `run.json`, `access/`,
logs and artifacts. `vivarium down` stops one and keeps the directory;
`vivarium rm` and `vivarium clean` delete it.
"""

from __future__ import annotations

import os
import pwd
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import anyio

from . import liveness, namespace, places, runroot
from .control import RUN_FILE, SOCKET, Op, RunFile, request
from .registry import ASK
from .spec import Spec, SpecError


class Refused(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Environment:
    name: str
    out: Path
    up: bool


async def answers(out: Path) -> bool:
    """Whether a run in *out* answers on its control socket."""
    try:
        with anyio.fail_after(ASK):
            await request(places.socket(out, SOCKET), Op.STATE)
    except (OSError, TimeoutError):
        return False
    return True


def starting(out: Path) -> bool:
    """Whether the process `run.json` names still runs: a run that is
    evaluating or booting has no socket yet. A reused pid errs on the side
    of keeping the directory; only a runner without `run.lock` gets here."""
    try:
        pid = RunFile.from_json((out / RUN_FILE).read_text()).pid
    except (OSError, ValueError, KeyError):
        return False
    # 0 or less signals a process group, never one process.
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running(out: Path) -> bool:
    """Whether the run in *out* runs: its lock, or for a runner without
    one, the pid in `run.json`."""
    found = liveness.life(out)
    if found is not liveness.Life.ABSENT:
        return found is liveness.Life.HELD
    return starting(out)


async def live(out: Path) -> bool:
    found = liveness.life(out)
    if found is not liveness.Life.ABSENT:
        return found is liveness.Life.HELD
    return await answers(out) or starting(out)


async def every() -> list[Environment]:
    root = places.state() / places.ENVIRONMENTS
    if not root.is_dir():
        return []
    found = []
    for out in sorted(root.iterdir()):
        if out.is_dir() and places.NAME.fullmatch(out.name):
            found.append(Environment(out.name, out, await live(out)))
    return found


def remove(out: Path) -> None:
    """Delete *out*, an environment's directory and nothing else.

    A container guest leaves files its subordinate ids own, which this user
    may not delete (nixidae/nixidae#1064). Those go through `unshare` from
    the run's spec, as the runner's own cleaner removes its run root."""
    if places.environment_name(out) is None:
        raise Refused(f"{out} is not under {places.state() / places.ENVIRONMENTS}; refusing to delete it")
    remover = _remover(out)
    try:
        shutil.rmtree(out)
    except PermissionError:
        if remover is None:
            raise
        subprocess.run([*remover, str(out)], check=True)  # noqa: S603 -- the run's own unshare, on its own directory
        if out.exists():
            raise Refused(f"{out} is still there after removal through a user namespace") from None


def _remover(out: Path) -> list[str] | None:
    try:
        about = RunFile.from_json((out / RUN_FILE).read_text())
    except (OSError, ValueError, KeyError):
        return None
    if about.test is None or about.test.spec is None:
        return None
    try:
        unshare = Spec.read(about.test.spec).unshare
    except (OSError, SpecError):
        return None
    if unshare is None:
        return None
    uid = os.getuid()
    subordinate = uid != 0 and namespace.has_subordinate_ids(pwd.getpwuid(uid).pw_name, uid)
    return [*namespace.user_argv(str(unshare), subordinate=subordinate), "--", sys.executable, "-c", runroot.REMOVE]


def why_not_up(out: Path, *, again: bool = True) -> str:
    """Why *out* has no live run: no such environment, or one that is down. With *again*, the
    `vivarium up` line that starts a down one again, from the origin its `run.json` keeps."""
    name = places.environment_name(out)
    if not (out / RUN_FILE).exists():
        return f"no environment is named {name}; `vivarium ps` lists them" if name else f"no run in {out}"
    what = f"the environment {name}" if name else f"the run in {out}"
    try:
        origin = RunFile.from_json((out / RUN_FILE).read_text()).origin
    except (OSError, ValueError, KeyError):
        origin = None
    if not again or origin is None:
        return f"{what} is down"
    where = ["--name", name] if name else ["--out", str(out)]
    line = shlex.join(["vivarium", "up", origin.attr, "--file", str(origin.file), *where])
    return f"{what} is down; start it again: {line}"
