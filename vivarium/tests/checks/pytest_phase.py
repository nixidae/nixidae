"""Is a pytest phase pytest, against real guests?

Reads the sandboxed run's `.attempt`; the phase fails on purpose. Each
test is a JUnit case of its own, the failure message is pytest's
rewritten assertion, the fixture's teardown ran on the guest, and a
journal entry and a command both name the test that caused them.

    pytest_phase ATTEMPT
"""

import sys
from pathlib import Path

from harness import Evidence, data

CASES = 8


def main() -> None:
    run = Evidence(Path(sys.argv[1]))
    run.want({"cases": "failed"})
    print("ok: the failing test failed the phase")

    cases = run.junit("pytest.cases")
    if len(cases) != CASES:
        run.fail(f"junit has {len(cases)} cases under pytest.cases, expected {CASES}")
    print(f"ok: {CASES} JUnit cases, one per test")

    failing = next((c for c in cases if "test_fails_on_purpose" in (c.get("name") or "")), None)
    failure = None if failing is None else failing.find("failure")
    message = "" if failure is None else failure.get("message") or ""
    if "assert '2' == '3'" not in message:
        run.fail(f"the failure is not pytest's rewritten assertion: {message!r}")
    print("ok: the failure message is the rewritten assertion")

    if not (run.out / "artifacts" / "one" / "fixture-teardown").is_file():
        run.fail("the async fixture's teardown never ran")
    print("ok: the fixture's teardown ran on the guest")

    events = run.events()
    logged = [str(data(e).get("case")) for e in events if e["kind"] == "journal" and e.get("text") == "from-a-test"]
    if not logged or not logged[0].endswith("::test_the_journal_names_the_test"):
        run.fail(f"the journal entry names {logged}")
    print(f"ok: the journal entry names {logged[0]}")

    if not any(
        e["kind"] == "rpc" and e.get("text") == "hostname" and str(data(e).get("case")).endswith("::test_hostname")
        for e in events
    ):
        run.fail("the command does not name its test")
    print("ok: and so does the command")

    # Logged by a test that returned at once. Without the phase's settle
    # it was lost to the teardown -- measured, before settle.
    if not any(e["kind"] == "journal" and e.get("text") == "logged-and-left" and e.get("phase") == "cases" for e in events):
        run.fail("a line logged as a test returned was lost")
    print("ok: a line logged on the way out still reached the phase")

    if any(e["kind"] == "journal" and data(e).get("identifier") == "vivarium-settle" for e in events):
        run.fail("the settle marker leaked into the events")
    print("ok: and the runner's own marker stayed out of them")


if __name__ == "__main__":
    main()
