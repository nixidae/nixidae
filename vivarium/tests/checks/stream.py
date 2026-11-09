"""Does a guest's journal survive the guest?

Reads the sandboxed run's `.attempt`. A unit logs a line, the phase
waits until the line is in the host-side file, then kills the guest with
SIGKILL. The line must be in `events.jsonl` as a journal event of
`probe.service`, attributed to `crash`, and in the raw stream in the
artifacts. The guest must have died without a shutdown, or the check
proves nothing about a crash.

    stream ATTEMPT
"""

import re
import sys
from pathlib import Path

from harness import Evidence, data

LINE = "streamed-before-the-crash"
CLEAN_SHUTDOWN = re.compile(r"Reached target.*Power-Off|reboot: ")


def main() -> None:
    run = Evidence(Path(sys.argv[1]))
    events = run.events()
    journal = [e for e in events if e["kind"] == "journal"]
    found = [
        e
        for e in journal
        if e.get("machine") == "one" and data(e).get("unit") == "probe.service" and e.get("text") == LINE
    ]
    if not found:
        run.fail(f"the line is not in events.jsonl as a journal event from probe.service; the last: {journal[-20:]}")
    print(f"ok: {found[0]}")
    if found[0].get("phase") != "crash":
        run.fail("the entry is not attributed to the phase that caused it")
    print("ok: attributed to the phase that logged it")
    if LINE not in (run.out / "artifacts" / "one" / "journal.jsonl").read_text():
        run.fail("the raw journal on the host lost the line")
    print("ok: and the raw stream is in the artifacts")
    if CLEAN_SHUTDOWN.search((run.out / "console" / "one.log").read_text()):
        run.fail("the guest shut down cleanly; crash() did not kill it")
    if any(e["kind"] == "rpc" and e.get("text") == "systemctl poweroff" for e in events):
        run.fail("teardown asked a dead guest to power off")
    print("ok: and the guest died without a shutdown")
    print(f"ok: {len(journal)} journal entries streamed in all")


if __name__ == "__main__":
    main()
