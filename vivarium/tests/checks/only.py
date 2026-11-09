"""Can a caller run one phase and leave the rest alone?

`--only only` keeps what `only` runs after (`boot`) and deselects the
rest. `never` and `later` fail if they ever run. A deselected phase does
not fail the run, and the selected one really ran: it leaves a file in
the artifacts.

    only VIVARIUM DRIVER WORKDIR
"""

import os
import sys
from pathlib import Path

from harness import Run

WANTED = {"only": "passed", "boot": "passed", "never": "deselected", "later": "deselected"}


def main() -> None:
    vivarium, driver, work = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    os.environ.setdefault("HOME", str(work))
    run = Run(vivarium, driver, work / "run", "--only", "only")
    run.wait("with --only only")
    run.want(WANTED)
    if not run.phases()["passed"]:
        run.fail("asking for one phase by name reported failure")
    print("ok: a deselected phase does not fail the run")
    if not (run.out / "artifacts" / "only-ran").is_file():
        run.fail("the phase was counted but never ran")
    print("ok: and it left its evidence in the artifacts")


if __name__ == "__main__":
    main()
