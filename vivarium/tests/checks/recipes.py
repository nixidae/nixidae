"""Does evidence get collected on the run that went wrong?

Reads the sandboxed run's `.attempt`. `boot` is the standard library's
recipe, which nothing declares. `cluster` fails on purpose; `evidence`
runs after it all the same (`always`) and leaves a file; `later` needs
only `evidence`, so the failure does not travel through it. The run
still fails: evidence is not an answer to the question `cluster` was
asked.

    recipes ATTEMPT
"""

import sys
from pathlib import Path

from harness import Evidence


def main() -> None:
    run = Evidence(Path(sys.argv[1]))
    run.want({"boot": "passed", "cluster": "failed", "evidence": "passed"})
    print("ok: evidence ran although the phase before it failed")
    if not (run.out / "artifacts" / "one" / "failed-units").is_file():
        run.fail("evidence passed and left nothing")
    run.want({"later": "passed"})
    if run.phases()["passed"]:
        run.fail("collecting evidence turned a failure into a pass")
    print("ok: and the run still failed")


if __name__ == "__main__":
    main()
