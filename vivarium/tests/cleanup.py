"""Does a run leave nothing behind, however it ends?

Each case starts `vivarium run` on one guest, signals the runner once a
phase has started, and looks for what is left: a run root, or a process
by its exact name. SIGKILL: the cleaner removes the root. SIGTERM: the
run exits 143 after the guest has powered itself off.

    cleanup VIVARIUM SPEC WORKDIR BACKEND
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

LEFT_BEHIND = {
    "uml": ("linux", "uml-passt-bridge", "passt"),
    "qemu": ("qemu-system-x86", "virtiofsd", "passt"),
    "krun": ("vivarium-krun", "passt"),
}
"""The runner's own children per backend, by the name the kernel keeps:
`comm`, cut at 15 bytes."""

POWERED_OFF = {
    "uml": "the kernel exited (0)",
    "qemu": "reboot: Power down",
    "krun": "reboot: Restarting system",
}
"""What a guest's console says when it powered itself off. A krun guest
resets instead, which is what ends libkrun (modules/qemu.nix)."""


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


def case(
    vivarium: str,
    spec: str,
    work: Path,
    sig: signal.Signals,
    left_behind: tuple[str, ...],
    *,
    keep: bool = False,
) -> int:
    name = f"{sig.name}{'-keep' if keep else ''}"
    out = work / f"run-{name}"
    log = work / f"run-{name}.log"
    env = {**os.environ, **({"VIVARIUM_KEEP": "1"} if keep else {})}
    with log.open("w") as sink:
        runner = subprocess.Popen(
            [vivarium, "run", "--spec", spec, "--out", str(out)],
            stdout=sink,
            stderr=subprocess.STDOUT,
            env=env,
        )

    def started() -> bool:
        if runner.poll() is not None:
            sys.exit(f"{sig.name}: the runner ended first:\n{log.read_text()}")
        return "[phase] hold" in log.read_text()

    if not until(started, 300, 0.2):
        sys.exit(f"{sig.name}: no phase started")
    runner.send_signal(sig)
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
        sys.exit(f"{sig.name}: left {roots()}")
    print(f"ok: {sig.name}, exit {status}, nothing left")
    return status


def main() -> None:
    vivarium, spec, work, backend = sys.argv[1], sys.argv[2], Path(sys.argv[3]), sys.argv[4]
    os.environ.setdefault("HOME", str(work))
    left_behind = LEFT_BEHIND[backend]
    case(vivarium, spec, work, signal.SIGKILL, left_behind, keep=True)
    case(vivarium, spec, work, signal.SIGKILL, left_behind)
    status = case(vivarium, spec, work, signal.SIGTERM, left_behind)
    # The runner catches SIGTERM and exits 128 + 15. Uncaught, Popen
    # would report -15.
    if status != 128 + signal.SIGTERM:
        sys.exit(f"SIGTERM: exit {status}, not 143")
    console = (work / "run-SIGTERM" / "console" / "one.log").read_text()
    if POWERED_OFF[backend] not in console:
        sys.exit("SIGTERM: the guest did not power itself off")
    print("ok: SIGTERM powered the guest off before the run ended")


if __name__ == "__main__":
    main()
