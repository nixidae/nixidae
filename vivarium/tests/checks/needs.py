"""checks.needs: a run whose guest needs what the host lacks ends before any guest boots.

Usage: needs.py DRIVER TMPDIR
"""

from __future__ import annotations

import sys
from pathlib import Path

from harness import Driven


def main() -> None:
    driver, tmp = sys.argv[1], Path(sys.argv[2])
    out = tmp / "out"
    driven = Driven(driver, out, "--no-control")
    # 2, no verdict: the run ends before its first phase, as for a spec it refuses.
    driven.wait("though its guest needs what no host has", status=2)
    log = driven.log.read_text()
    if "Traceback" in log:
        driven.fail("the refusal prints a traceback, not its message")
    for want in (
        "nothing booted",
        "absent-device (/dev/vivarium-absent does not exist)",
        "absent-path (/proc/vivarium-absent does not exist)",
        "fix: none: the check expects this to be missing",
    ):
        if want not in log:
            driven.fail(f"the refusal does not say {want!r}")
        print(f"ok: the refusal says {want!r}")
    if "present-path" in log:
        driven.fail("the refusal names a need the host meets")
    print("ok: a need the host meets is not named")
    if "booting one" in log or (out / "console" / "one.log").exists():
        driven.fail("a guest began to boot")
    print("ok: no guest began to boot")


if __name__ == "__main__":
    main()
