"""Does `--only` boot only what its phases need?

`check` runs on `a`, and `a` requires `b`. `other` runs on `c`, and
`--only check` deselects it, so `c` never boots. A pause before `boot`
has the guests of `check` up, though under `--only` no phase has booted
them yet.

    on_demand VIVARIUM DRIVER WORKDIR
"""

import os
import sys
from pathlib import Path

from harness import Run


def only(vivarium: str, driver: str, work: Path) -> None:
    run = Run(vivarium, driver, work / "run", "--only", "check")
    run.wait("with --only check")
    if not run.phases()["passed"]:
        run.fail(f"the run failed: {run.phase_states()}")
    events = run.events()
    booted = [str(e["text"]) for e in events if e["kind"] == "boot"]
    if "booting a, b" not in booted:
        run.fail(f"a and b did not boot together: {booted}")
    if any("c" in text.removeprefix("booting ").split(", ") for text in booted):
        run.fail(f"c booted, and no selected phase needs it: {booted}")
    # `prepare` resolves every guest's forwards and says so; past that
    # line, c's console is empty only if c never ran.
    console = [
        e
        for e in events
        if e.get("machine") == "c" and e["kind"] == "console" and not str(e["text"]).startswith("forwarding ")
    ]
    if console:
        run.fail(f"c wrote {len(console)} console lines, so it ran")
    print("ok: --only check booted a and b, and not c")


def pause_before_boot(vivarium: str, driver: str, work: Path) -> None:
    run = Run(vivarium, driver, work / "pause", "--only", "check", "--break", "boot")
    run.wait_for_pause()
    up = run.ok("exec", "sorted(vms)").strip()
    if up != "['a', 'b']":
        run.fail(f"the pause before boot has the wrong guests up: {up}")
    run.ok("continue")
    run.wait("after the pause")
    print("ok: paused before boot with a and b up")


def main() -> None:
    vivarium, driver, work = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    os.environ.setdefault("HOME", str(work))
    only(vivarium, driver, work)
    pause_before_boot(vivarium, driver, work)


if __name__ == "__main__":
    main()
