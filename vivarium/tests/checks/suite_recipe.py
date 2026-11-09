"""Does the `suites` recipe run pytest inside a guest?

The suite holds one passing and one failing test. The sandboxed run's
`.attempt` fails, and both tests are cases of the guest. A driver run
given `-- -k test_ok` passes, so the driver's arguments reach the pytest
in the guest.

    suite_recipe ATTEMPT DRIVER WORKDIR
"""

import os
import sys
from pathlib import Path

from harness import Driven, Evidence, data


def main() -> None:
    attempt, driver, work = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    os.environ.setdefault("HOME", str(work))
    run = Evidence(attempt)
    if run.status() != 1:
        run.fail("a suite with a failing test passed")
    cases = run.cases()
    if not any(
        data(c).get("outcome") == "failed" and c.get("machine") == "one" and c.get("phase") == "inner" for c in cases
    ):
        run.fail(f"the failing test is not a failed case of the phase: {cases}")
    if not any(data(c).get("outcome") == "passed" for c in cases):
        run.fail(f"the passing test is not a case: {cases}")
    log = run.out / "artifacts" / "one" / "inner.log"
    if not log.is_file() or not log.stat().st_size:
        run.fail("the suite's output is not in the artifacts")
    print("ok: the suite ran in the guest, and its tests are cases")

    Driven(driver, work / "k", "--", "-k", "test_ok").wait("with -- -k test_ok: the argument did not reach the guest")
    print("ok: the driver's -- arguments reached the suite")


if __name__ == "__main__":
    main()
