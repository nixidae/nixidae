"""Does `report.json` hold each guest's launch, its whole boot, and the teardown?

Reads the sandboxed run's `.attempt`, a run of two guests, `a` and `b`.

- Each guest has a `launch` step, and its `boot` step starts with it. The
  guests launch one after another, so a boot clock started at the spawn
  gave every guest the start of the last launch.
- Each guest has a `reap` and a `clean up` step: the report is written
  again after the teardown.
- No step ends after `total_seconds`.

    report ATTEMPT
"""

from __future__ import annotations

import sys
from pathlib import Path

from harness import Evidence, Step

GUESTS = ("a", "b")


def one(steps: list[Step], kind: str, what: str) -> Step | None:
    found = [s for s in steps if s["kind"] == kind and s["what"] == what]
    return found[0] if len(found) == 1 else None


def main() -> None:
    run = Evidence(Path(sys.argv[1]))
    if run.status() != 0:
        run.fail("the run failed")
    report = run.report()
    steps = report["steps"]
    for guest in GUESTS:
        launch = one(steps, "launch", f"launch {guest}")
        boot = one(steps, "boot", f"boot {guest}")
        if launch is None or boot is None:
            run.fail(f"{guest}: expected one launch and one boot step: {steps}")
        # `at` is rounded to milliseconds, and each step rounds its own.
        if abs(boot["at"] - launch["at"]) > 0.002 or boot["seconds"] < launch["seconds"]:
            run.fail(f"{guest}: the boot does not start with its launch: {launch} {boot}")
        print(f"ok: {guest} launched at {launch['at']}s in {launch['seconds']}s, and its boot starts there")
        for what in (f"reap {guest}", f"clean up {guest}"):
            if one(steps, "teardown", what) is None:
                run.fail(f"no teardown step {what!r}: {steps}")
        print(f"ok: {guest}'s teardown is in the report")
    late = [s for s in steps if s["at"] + s["seconds"] > report["total_seconds"] + 0.002]
    if late:
        run.fail(f"steps end after total_seconds {report['total_seconds']}: {late}")
    print("ok: no step ends after the total")


if __name__ == "__main__":
    main()
