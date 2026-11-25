"""checks.kept: two runs of one environment, then a run that keeps nothing.

Usage: kept.py DRIVER TMPDIR
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from harness import Driven


def run(driver: str, out: Path) -> Driven:
    driven = Driven(driver, out, "--no-control")
    driven.wait(f"in {out}")
    driven.want({"count": "passed"})
    return driven


def main() -> None:
    driver, tmp = sys.argv[1], Path(sys.argv[2])
    # Where `places` looks for environments: this check's own.
    os.environ["VIVARIUM_STATE"] = str(tmp / "state")
    environment = tmp / "state" / "envs" / "kept"
    environment.parent.mkdir(parents=True)
    for count in (1, 2):
        run(driver, environment)
        kept = json.loads((environment / "state.json").read_text())
        if kept != {"keys": {"runs": count}, "outcomes": {"boot": "passed", "count": "passed"}}:
            sys.exit(f"after run {count}, state.json is {kept}")
        print(f"ok: run {count} of the environment counted {count}")
    disk = environment / "disks" / "one"
    if not disk.exists():
        sys.exit(f"no kept store at {disk}")
    print(f"ok: the store is {disk}")

    plain = tmp / "plain"
    run(driver, plain)
    if (plain / "state.json").exists() or (plain / "disks").exists():
        sys.exit(f"a plain --out kept something: {sorted(p.name for p in plain.iterdir())}")
    print("ok: a plain --out started blank and kept nothing")


if __name__ == "__main__":
    main()
