"""verdict.py ATTEMPT OUT NAME=WHAT...: a check's builder, which reads its attempt's verdict.

The attempt never fails, so Nix keeps what a failed run left. This fails
the check on a `status` other than 0, after the tail of the log. On a pass
it links the attempt, and each NAME in it, into OUT.
"""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

TAIL = 50


def main(argv: list[str]) -> int:
    attempt, out, *named = argv
    run = Path(attempt)
    entries = dict(entry.split("=", 1) for entry in named)
    print(f"the run is at {run}")
    width = max(map(len, entries), default=0)
    for name, what in entries.items():
        print(f"  {name.ljust(width)}  {what}")

    status = (run / "status").read_text().strip()
    if status != "0":
        with (run / "log").open(errors="replace") as log:
            tail = deque(log, maxlen=TAIL)
        print(f"\n--- the last {TAIL} lines of {run}/log ---")
        print("".join(tail), end="")
        print("--- end ---\n", flush=True)
        print(f"the run failed (exit {status}); the paths above hold what it left", file=sys.stderr)
        return 1

    result = Path(out)
    result.mkdir()
    (result / "attempt").symlink_to(run)
    # Every name it was given, so a sink the runner adds needs only its
    # entry in the list to be reachable from `result/`.
    for name in entries:
        if (run / name).exists():
            (result / name).symlink_to(run / name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
