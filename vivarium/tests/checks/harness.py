"""What the check scripts here share: a driver in the background and `ctl` against it.

A check runs `python3 <this directory>/<check>.py`, so this module
imports by name. Standard library only: a check runs on a bare
interpreter, as a person would run the driver and the CLI.
"""

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
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


class Evidence:
    """What a finished run left in its output directory: `--out`, or a
    test's `.attempt`."""

    def __init__(self, out: Path) -> None:
        self.out = out

    def fail(self, why: str) -> NoReturn:
        sys.exit(f"{why}\n--- phases.json\n{_read(self.out / 'phases.json')}")

    def events(self) -> list[dict[str, object]]:
        # Dicts, not a model: each kind of event carries its own `data`.
        return [json.loads(line) for line in (self.out / "events.jsonl").read_text().splitlines()]

    def phases(self) -> Phases:
        return cast("Phases", json.loads((self.out / "phases.json").read_text()))

    def phase_states(self) -> dict[str, str]:
        return {phase["name"]: phase["state"] for phase in self.phases()["phases"]}

    def status(self) -> int:
        """`<out>/status`: 0 passed, 1 did not."""
        return int((self.out / "status").read_text())

    def cases(self) -> list[dict[str, object]]:
        return [e for e in self.events() if e["kind"] == "case"]

    def junit(self, classname: str) -> list[ET.Element]:
        """The run's junit.xml cases of one class."""
        root = ET.parse(self.out / "junit.xml").getroot()
        return [case for case in root.iter("testcase") if case.get("classname") == classname]

    def want(self, states: dict[str, str]) -> None:
        """Each phase in the state named; one `ok:` line each."""
        found = self.phase_states()
        for name, wanted in states.items():
            if found.get(name) != wanted:
                self.fail(f"phase {name} is {found.get(name)!r}, expected {wanted!r}")
            print(f"ok: {name} is {wanted}")


class Driven(Evidence):
    """One driver in the background, its output in `<out>.log`."""

    def __init__(self, driver: str, out: Path, *flags: str) -> None:
        super().__init__(out)
        self.log = out.with_suffix(".log")
        with self.log.open("w") as sink:
            self.process = subprocess.Popen([driver, "--out", str(out), *flags], stdout=sink, stderr=subprocess.STDOUT)

    def fail(self, why: str) -> NoReturn:
        self.process.kill()
        sys.exit(f"{why}\n--- {self.log.name}\n{self.log.read_text()}")

    def wait(self, why: str, status: int = 0) -> None:
        """The run's exit; the check fails unless it is `status`."""
        if self.process.wait(timeout=1800) != status:
            self.fail(f"the run exited {self.process.returncode}, not {status}, {why}")

    def exited(self) -> int:
        return self.process.wait(timeout=1800)


class Run(Driven):
    """A driver in the background, and `ctl` and `monitor` against it."""

    def __init__(self, vivarium: str, driver: str, out: Path, *flags: str) -> None:
        super().__init__(driver, out, *flags)
        self.vivarium = vivarium

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


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError as error:
        return f"({error})"


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
