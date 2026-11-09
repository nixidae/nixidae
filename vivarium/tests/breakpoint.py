"""Can a person reach into a paused run, and end it?

Driven with the real binaries, the way a person or an agent drives them:
the driver with `--break` in the background, `vivarium ctl` against its
socket. The file injected and the tests sent by hand are written here,
in the build directory, and were never in the store: that is the point
of `inject` and `ctl pytest`.

- paused before `later`: `exec`, a namespace that survives, an exception
  as the reply, `inject`, `pytest` by hand and then its edit, `continue`;
- `--break end` pauses after the last phase;
- `ctl stop` ends a paused run: it returns once the run has exited, the
  run exits 5, the verdict says stopped, and no guest process is left.

    breakpoint VIVARIUM DRIVER WORKDIR
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

STOPPED = 5
GUEST_PROCESSES = ("linux", "uml-passt-bridge", "passt")
"""A UML guest's processes, by `comm`."""

INJECTED = """\
from vivarium_runner import Machines

async def test(vms: Machines) -> None:
    await vms.one.succeed("echo injected > /artifacts/injected")
    print("[scratch] wrote it")
"""

BY_HAND = 'async def test_host(one): assert (await one.succeed("hostname")).strip() == "{name}"\n'


class Run:
    """One driver in the background, and `ctl` against it."""

    def __init__(self, vivarium: str, driver: str, out: Path, *flags: str) -> None:
        self.vivarium = vivarium
        self.out = out
        self.log = out.with_suffix(".log")
        with self.log.open("w") as sink:
            self.process = subprocess.Popen(
                [driver, "--out", str(out), *flags], stdout=sink, stderr=subprocess.STDOUT
            )

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

    def wait_for_pause(self) -> None:
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            done = self.ctl("state")
            if done.returncode == 0 and done.stdout.splitlines()[:1] == ["paused"]:
                return
            if self.process.poll() is not None:
                self.fail("the run ended without pausing")
            time.sleep(0.1)
        self.fail("the run never paused")

    def states(self) -> dict[str, str]:
        rows = (line.split("\t", 1) for line in self.ok("state").splitlines()[1:])
        return {name: state for name, state in rows}

    def events(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in (self.out / "events.jsonl").read_text().splitlines()]


def by_hand(vivarium: str, driver: str, work: Path) -> None:
    run = Run(vivarium, driver, work / "run", "--break", "later")
    run.wait_for_pause()
    if run.states().get("later") != "pending":
        run.fail("the phase ran through its breakpoint")
    print("ok: paused before later")

    if "one" not in run.ok("exec", 'await one.succeed("hostname")'):
        run.fail("exec did not reach the guest")
    print("ok: exec reached the guest")

    run.ok("exec", 'n = (await one.succeed("echo 7")).strip()')
    if run.ok("exec", "n").strip() != "'7'":
        run.fail("a name did not survive to the next exec")
    print("ok: the namespace survives between calls")

    raised = run.ctl("exec", "1/0")
    if raised.returncode == 0:
        run.fail("an exception was reported as success")
    if "ZeroDivisionError" not in raised.stderr:
        run.fail(f"the traceback did not come back: {raised.stderr}")
    print("ok: an exception comes back as the reply, and the run stays up")

    scratch = work / "scratch.py"
    scratch.write_text(INJECTED)
    if "wrote it" not in run.ok("inject", str(scratch)):
        run.fail("inject did not run the file")
    print("ok: a file from outside the store ran against the guest")

    tree = work / "tree"
    tree.mkdir()
    (tree / "test_by_hand.py").write_text(BY_HAND.format(name="one"))
    if "1 passed" not in run.ok("pytest", str(tree)).splitlines():
        run.fail("pytest by hand did not pass against the guest")
    (tree / "test_by_hand.py").write_text(BY_HAND.format(name="edited"))
    if run.ctl("pytest", str(tree), "--", "-k", "host").returncode == 0:
        run.fail("pytest by hand ran the old test after an edit")
    print("ok: pytest ran a working tree's test, then its edit, against the paused guest")

    run.ok("continue")
    if run.process.wait(timeout=600) != 0:
        run.fail(f"the run exited {run.process.returncode} after continue")
    print("ok: continue finished the run")

    phases = json.loads((run.out / "phases.json").read_text())
    if {p["name"]: p["state"] for p in phases["phases"]}.get("later") != "passed":
        run.fail("later did not run after continue")
    if not (run.out / "artifacts" / "one" / "injected").is_file():
        run.fail("the injected write is not on the host")
    events = run.events()
    if not any(e["kind"] == "output" and e.get("phase") == "inject:scratch.py" for e in events):
        run.fail("what the injected file printed is not in the events")
    if not any(e["kind"] == "note" and _data(e).get("op") == "exec" for e in events):
        run.fail("the exec is not recorded in the events")
    print("ok: and events.jsonl records what was done by hand")

    if (run.out / "status").read_text().strip() != "0":
        run.fail("a failing test sent by hand failed the run")
    if "test_by_hand" in (run.out / "junit.xml").read_text():
        run.fail("a test sent by hand is in junit.xml")
    if not any(e["kind"] == "case" and _data(e).get("by_hand") and _data(e).get("outcome") == "failed" for e in events):
        run.fail("the by-hand case is not in the events")
    print("ok: and its cases are events, outside the verdict and junit.xml")
    if (run.out / "control.sock").exists():
        run.fail("the socket outlived the run")


def break_at_end(vivarium: str, driver: str, work: Path) -> None:
    """A point, not a phase: `end` pauses after the last phase."""
    run = Run(vivarium, driver, work / "end", "--break", "end")
    run.wait_for_pause()
    if run.states().get("later") != "passed":
        run.fail("--break end paused before the last phase ran")
    run.ok("continue")
    if run.process.wait(timeout=600) != 0:
        run.fail(f"the run exited {run.process.returncode} after continue at end")
    print("ok: --break end paused after the last phase")


def stop(vivarium: str, driver: str, work: Path) -> None:
    run = Run(vivarium, driver, work / "stop", "--break", "later")
    run.wait_for_pause()
    if run.ok("stop").strip() != "stopped":
        run.fail("ctl stop did not wait for the run to end")
    if run.process.poll() is None:
        run.fail("the run is still up after ctl stop returned")
    if run.process.returncode != STOPPED:
        run.fail(f"a stopped run exited {run.process.returncode}, not {STOPPED}")
    if json.loads((run.out / "phases.json").read_text())["stopped"] != "ctl stop":
        run.fail("phases.json does not say stopped")
    if (run.out / "status").read_text().strip() != "1":
        run.fail("a stopped run reads as passed")
    if not any(e["kind"] == "run_finished" and _data(e).get("stopped") == "ctl stop" for e in run.events()):
        run.fail("the verdict event does not say stopped")
    if (run.out / "control.sock").exists():
        run.fail("the socket outlived a stopped run")
    if left := running(GUEST_PROCESSES):
        run.fail(f"guest processes are left after stop: {', '.join(left)}")
    monitor = subprocess.run(
        [vivarium, "monitor", str(run.out), "--quiet", "--wait", "5"], capture_output=True, text=True, timeout=60
    )
    if monitor.returncode != STOPPED:
        run.fail(f"the monitor read a stopped run as {monitor.returncode}, not {STOPPED}: {monitor.stdout}")
    print("ok: ctl stop tore the guests down and the run says stopped")


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


def _data(event: dict[str, object]) -> dict[str, object]:
    data = event.get("data")
    return data if isinstance(data, dict) else {}


def main() -> None:
    vivarium, driver, work = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    os.environ.setdefault("HOME", str(work))
    by_hand(vivarium, driver, work)
    break_at_end(vivarium, driver, work)
    stop(vivarium, driver, work)


if __name__ == "__main__":
    main()
