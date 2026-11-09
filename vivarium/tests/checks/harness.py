"""What the check scripts here share: a driver in the background and `ctl` against it.

A check runs `python3 <this directory>/<check>.py`, so this module
imports by name. Standard library only: a check runs on a bare
interpreter, as a person would run the driver and the CLI.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import NoReturn, TypedDict, cast

PAUSED = 4
"""`vivarium monitor`'s exit status at a pause."""

UML_PROCESSES = ("linux", "uml-passt-bridge", "passt")
"""A UML guest's processes, by `comm`."""


class State(TypedDict):
    """`ctl --json state`: `vivarium.control.Reply`, as JSON."""

    ok: bool
    result: str | None
    state: dict[str, str] | None
    pause: str | None
    errors: dict[str, str] | None


class Phase(TypedDict):
    name: str
    state: str
    error: str | None


class Phases(TypedDict):
    """`<out>/phases.json`."""

    passed: bool
    stopped: str | None
    phases: list[Phase]


class Run:
    """One driver in the background, and `ctl` against it."""

    def __init__(self, vivarium: str, driver: str, out: Path, *flags: str) -> None:
        self.vivarium = vivarium
        self.out = out
        self.log = out.with_suffix(".log")
        with self.log.open("w") as sink:
            self.process = subprocess.Popen([driver, "--out", str(out), *flags], stdout=sink, stderr=subprocess.STDOUT)

    def fail(self, why: str) -> NoReturn:
        self.process.kill()
        sys.exit(f"{why}\n--- {self.log.name}\n{self.log.read_text()}")

    def ctl(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.vivarium, "ctl", "--out", str(self.out), *argv], capture_output=True, text=True, timeout=600
        )

    def ok(self, *argv: str) -> str:
        """`ctl`'s stdout; the check fails if `ctl` does."""
        done = self.ctl(*argv)
        if done.returncode != 0:
            self.fail(f"ctl {' '.join(argv)} exited {done.returncode}: {done.stdout}{done.stderr}")
        return done.stdout

    def state(self) -> State:
        return cast("State", json.loads(self.ok("--json", "state")))

    def wait_for_pause(self) -> State:
        """The state at the pause. The monitor exits 4 when the run pauses,
        so nothing here polls."""
        watched = subprocess.run(
            [self.vivarium, "monitor", str(self.out), "--quiet", "--wait", "300"],
            capture_output=True,
            text=True,
            timeout=900,
        )
        if watched.returncode != PAUSED:
            self.fail(f"the run did not pause: the monitor exited {watched.returncode}: {watched.stdout}")
        return self.state()

    def wait(self, why: str) -> None:
        """The run's exit; the check fails unless it passed."""
        if self.process.wait(timeout=600) != 0:
            self.fail(f"the run exited {self.process.returncode} {why}")

    def events(self) -> list[dict[str, object]]:
        # Dicts, not a model: each kind of event carries its own `data`.
        return [json.loads(line) for line in (self.out / "events.jsonl").read_text().splitlines()]

    def phases(self) -> Phases:
        return cast("Phases", json.loads((self.out / "phases.json").read_text()))

    def phase_states(self) -> dict[str, str]:
        return {phase["name"]: phase["state"] for phase in self.phases()["phases"]}


def data(event: dict[str, object]) -> dict[str, object]:
    found = event.get("data")
    return found if isinstance(found, dict) else {}


def running(names: tuple[str, ...]) -> list[str]:
    found = []
    for comm in Path("/proc").glob("[0-9]*/comm"):
        try:
            name = comm.read_text().strip()
        except OSError:
            continue
        if name in names:
            found.append(f"{name} ({comm.parent.name})")
    return found
