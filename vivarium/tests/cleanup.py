"""Does a run leave nothing behind, however it ends?

Each case starts `vivarium run` on one guest and looks for what is left:
a run root, or a process by its exact name. A run that ends by itself:
its teardown takes everything down. The rest signal the runner once a
phase has started. SIGKILL: the cleaner removes the root. SIGTERM: the
run exits 143 after the guest has powered itself off, or with
`--no-control` after it was killed. `ctl stop` while the phase runs: the
run exits 5 and says stopped.

    cleanup VIVARIUM SPEC WORKDIR BACKEND
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final, Literal

EGRESS: Final = "egress"
"""The guest's uplink on the host: `vivarium.uplink.proxy = "vsock"`."""

LEFT_BEHIND = {
    "uml": ("linux", EGRESS),
    "qemu": ("qemu-system-x86", "virtiofsd", EGRESS),
    "krun": ("vivarium-krun", EGRESS),
}
"""The runner's own children per backend, by the name the kernel keeps:
`comm`, cut at 15 bytes."""

STOPPED: Final = 5
"""The exit status of a run that `ctl stop` ended."""

POWERED_OFF: Final = "reboot: Power down"
"""What a guest's console says when it powered itself off, on every
backend. A krun guest on x86 powers off through libkrun's ACPI S5
(modules/qemu.nix)."""


def roots() -> list[Path]:
    places = {Path("/tmp"), Path(tempfile.gettempdir())}
    return sorted(root for place in places for root in place.glob("vivarium-run-*"))


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


def until(check: Callable[[], bool], seconds: float, step: float = 0.1) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(step)
    return check()


STOP: Final = "ctl-stop"
"""End the run with `vivarium ctl stop` rather than a signal."""


def case(
    vivarium: str,
    spec: str,
    work: Path,
    end: signal.Signals | Literal["ctl-stop"],
    left_behind: tuple[str, ...],
    *,
    keep: bool = False,
    flags: tuple[str, ...] = (),
) -> int:
    """*flags* go to `vivarium run`."""
    how = end if isinstance(end, str) else end.name
    name = f"{how}{'-keep' if keep else ''}{''.join(flags)}"
    out = work / f"run-{name}"
    log = work / f"run-{name}.log"
    env = {**os.environ, **({"VIVARIUM_KEEP": "1"} if keep else {})}
    with log.open("w") as sink:
        runner = subprocess.Popen(
            [vivarium, "run", "--spec", spec, "--out", str(out), *flags],
            stdout=sink,
            stderr=subprocess.STDOUT,
            env=env,
        )

    def started() -> bool:
        if runner.poll() is not None:
            sys.exit(f"{how}: the runner ended first:\n{log.read_text()}")
        return "[phase] hold" in log.read_text()

    if not until(started, 300, 0.2):
        sys.exit(f"{how}: no phase started")
    if isinstance(end, str):
        # While `hold` runs: the drive is cancelled from the socket's
        # handler, not from a signal handler, and must still tear down.
        stopped = subprocess.run([vivarium, "ctl", "--out", str(out), "stop"], capture_output=True, text=True)
        if stopped.returncode != 0 or stopped.stdout.strip() != "stopped":
            sys.exit(f"{how}: ctl stop exited {stopped.returncode}: {stopped.stdout}{stopped.stderr}")
    else:
        runner.send_signal(end)
    status = runner.wait()
    # Keeping the root keeps files, never processes.
    if until(lambda: not running(left_behind), 5) is False:
        sys.exit(f"{name}: left {', '.join(running(left_behind))}")
    if keep:
        # The negative control: with removal off, this check must see
        # the root, or its "nothing left" means nothing.
        if not until(lambda: bool(roots()), 5):
            sys.exit(f"{name}: nothing left although removal was off")
        for root in roots():
            shutil.rmtree(root)
        print(f"ok: {name}, the root was left, as it must be")
        return status
    # The cleaner removes the root just after the runner is gone.
    if not until(lambda: not roots(), 5):
        sys.exit(f"{how}: left {roots()}")
    print(f"ok: {how}, exit {status}, nothing left")
    return status


def ends_by_itself(vivarium: str, spec: str, work: Path, left_behind: tuple[str, ...]) -> None:
    """A run that passes and ends: `--only boot` deselects `hold`. Its teardown
    alone must take every guest process down; no signal helps it."""
    out = work / "run-ends"
    log = work / "run-ends.log"
    with log.open("w") as sink:
        status = subprocess.run(
            [vivarium, "run", "--spec", spec, "--out", str(out), "--only", "boot"],
            stdout=sink,
            stderr=subprocess.STDOUT,
            check=False,
        ).returncode
    if status != 0:
        sys.exit(f"ends: exit {status}:\n{log.read_text()}")
    if not until(lambda: not running(left_behind), 5):
        sys.exit(f"ends: left {', '.join(running(left_behind))}")
    if not until(lambda: not roots(), 5):
        sys.exit(f"ends: left {roots()}")
    print("ok: a run that ended by itself left nothing")


def main() -> None:
    vivarium, spec, work, backend = sys.argv[1], sys.argv[2], Path(sys.argv[3]), sys.argv[4]
    os.environ.setdefault("HOME", str(work))
    left_behind = LEFT_BEHIND[backend]
    ends_by_itself(vivarium, spec, work, left_behind)
    case(vivarium, spec, work, signal.SIGKILL, left_behind, keep=True)
    case(vivarium, spec, work, signal.SIGKILL, left_behind)
    status = case(vivarium, spec, work, signal.SIGTERM, left_behind)
    # The runner catches SIGTERM and exits 128 + 15. Uncaught, Popen
    # would report -15.
    if status != 128 + signal.SIGTERM:
        sys.exit(f"SIGTERM: exit {status}, not 143")
    console = (work / "run-SIGTERM" / "console" / "one.log").read_text()
    if POWERED_OFF not in console:
        sys.exit("SIGTERM: the guest did not power itself off")
    print("ok: SIGTERM powered the guest off before the run ended")

    # A check's run: nobody can reach the guest, so it is killed, not powered off.
    status = case(vivarium, spec, work, signal.SIGTERM, left_behind, flags=("--no-control",))
    if status != 128 + signal.SIGTERM:
        sys.exit(f"SIGTERM --no-control: exit {status}, not 143")
    console = (work / "run-SIGTERM--no-control" / "console" / "one.log").read_text()
    if POWERED_OFF in console:
        sys.exit("SIGTERM --no-control: the guest powered itself off, it was not killed")
    print("ok: SIGTERM with no control socket killed the guest")

    status = case(vivarium, spec, work, STOP, left_behind)
    if status != STOPPED:
        sys.exit(f"{STOP}: exit {status}, not {STOPPED}")
    phases = json.loads((work / f"run-{STOP}" / "phases.json").read_text())
    states = {phase["name"]: phase["state"] for phase in phases["phases"]}
    if phases["stopped"] != "ctl stop" or states.get("hold") != "interrupted":
        sys.exit(f"{STOP}: phases.json says {phases}")
    print("ok: ctl stop while a phase ran: exit 5, hold interrupted, the run says stopped")


if __name__ == "__main__":
    main()
