"""Can a suite that runs inside a guest report like one on the host?

Reads the sandboxed run's `.attempt`. The guest writes JUnit to
`/artifacts/junit/*.xml`; each test becomes a case event with its
machine and phase, and a case in the run's own junit.xml. One script
serves both phases through `vms.phase`, and a value crosses between
them through `vms.shared`.

    guest_suites ATTEMPT
"""

import sys
from pathlib import Path

from harness import Evidence, data


def main() -> None:
    run = Evidence(Path(sys.argv[1]))
    if run.status() != 0:
        run.fail("the run failed; the script's verdict is the phase's, not the cases'")
    print("ok: both phases passed, one script serving both")

    cases = run.cases()
    if len(cases) != 2:
        run.fail(f"expected the two cases the guest wrote, found {cases}")
    if not any(
        c["text"] == "inner.test_a::test_bad"
        and data(c).get("outcome") == "failed"
        and c.get("machine") == "one"
        and c.get("phase") == "suite"
        for c in cases
    ):
        run.fail(f"the failed case lost its outcome, machine or phase: {cases}")
    print("ok: the guest's JUnit became cases, with machine and phase")

    junit = run.junit("guest-suites.suite")
    if len(junit) != 2:
        run.fail(f"junit.xml holds {len(junit)} of the guest's cases, expected 2")
    bad = next((c for c in junit if c.get("name") == "inner.test_a::test_bad"), None)
    failure = None if bad is None else bad.find("failure")
    if failure is None or "assert 1 == 2" not in (failure.get("message") or ""):
        run.fail("the failure message did not survive")
    print("ok: and they are cases in the run's own junit.xml")


if __name__ == "__main__":
    main()
