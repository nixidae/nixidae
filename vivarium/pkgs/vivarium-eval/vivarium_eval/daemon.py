"""`up --daemonize`: the run as a transient unit of the user's systemd.

Detached alone (`setsid`), a run stays in its caller's cgroup: stopping
the caller's scope, a CI job or an agent's session, stops the environment
with it (measured: a plain `up` in `systemd-run --user --scope` went down
with the scope). A unit of its own outlives the caller, shows in
`systemctl --user status vivarium-NAME`, and ends at logout unless the
user enabled lingering.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import TYPE_CHECKING, Final

import anyio
from vivarium import places
from vivarium_runner import container

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

SYSTEMD_OWN: Final = frozenset(
    {
        "NOTIFY_SOCKET",
        "INVOCATION_ID",
        "JOURNAL_STREAM",
        "LISTEN_PID",
        "LISTEN_FDS",
        "LISTEN_FDNAMES",
        "MANAGERPID",
        "SYSTEMD_EXEC_PID",
        "WATCHDOG_PID",
        "WATCHDOG_USEC",
        "MAINPID",
    }
)
"""What systemd sets for a unit itself; the caller's would name the caller's unit."""

NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

PROPERTIES: Final = (
    # `systemd-run` returns once the run is exec'd, so MainPID is the run's.
    "Type=exec",
    # The run tears its guests down on SIGTERM; the rest of the unit only
    # after that, or at the timeout. Teardown measured up to 20 s.
    "KillMode=mixed",
    "TimeoutStopSec=90",
    # A service's default stops the whole unit when the kernel kills one
    # of its processes; a guest that runs out of memory is the test's news.
    "OOMPolicy=continue",
)
"""No `Delegate=`: the container launcher makes each guest a scope of its
own outside the unit, and stopping the unit tears those down too (measured)."""


class DaemonError(RuntimeError):
    pass


def unit_name(out: Path) -> str:
    environment = places.environment_name(out)
    return f"vivarium-{environment or places.key(out)}.service"


def passed(environ: Mapping[str, str]) -> list[str]:
    """The variables the unit takes from `systemd-run`'s own environment,
    by name: the run evaluates, so it needs the caller's, and a value on
    the command line is readable by every user in /proc."""
    return sorted(name for name in environ if NAME.fullmatch(name) and name not in SYSTEMD_OWN)


def argv(
    systemd_run: str, unit: str, description: str, cwd: Path, log: Path, names: Iterable[str], command: list[str]
) -> list[str]:
    words = [systemd_run, "--user", "--unit", unit, "--description", description, "--collect", "--quiet"]
    words += ["--working-directory", str(cwd), "-p", f"StandardOutput=truncate:{log}", "-p", "StandardError=inherit"]
    for each in PROPERTIES:
        words += ["-p", each]
    for name in names:
        words += ["-E", name]
    return [*words, "--", *command]


def manager() -> str | None:
    """`systemd-run`, when the user's systemd answers; as the container
    launcher decides it, by making a delegated scope."""
    prefix = anyio.run(container.scope, None)
    return None if prefix is None else prefix[0]


def start(
    systemd_run: str, out: Path, log: Path, cwd: Path, environ: Mapping[str, str], command: list[str]
) -> tuple[str, int]:
    """Start *command* as a unit in *cwd*, the caller's: a relative
    `--file` names a place from there. Its name and main pid."""
    unit = unit_name(out)
    name = places.environment_name(out) or str(out)
    words = argv(systemd_run, unit, f"vivarium environment {name}", cwd, log, passed(environ), command)
    done = subprocess.run(words, check=False)  # noqa: S603 -- the host's systemd-run with this program
    if done.returncode != 0:
        raise DaemonError(f"systemd-run could not start {unit} (exit {done.returncode})")
    systemctl = shutil.which("systemctl") or "systemctl"
    shown = subprocess.run(  # noqa: S603 -- the host's systemctl, reading one property
        [systemctl, "--user", "show", "--property", "MainPID", "--value", unit],
        capture_output=True,
        text=True,
        check=False,
    )
    pid = int(shown.stdout.strip() or 0)
    if pid <= 0:
        # `--collect` drops a unit that ended at once; its log says why.
        raise DaemonError(f"{unit} ended as it started; see {log}")
    return unit, pid
