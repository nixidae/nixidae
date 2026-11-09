"""Does `--kernel` boot the kernel it names?

A copy of the Nix-built kernel outside the store stands in for a working
tree's `linux`. The copy boots and the run passes, and the run records
the override. A path that is not a kernel fails the boot, so what boots
is the override and not the spec's kernel behind it.

    kernel_override SPEC DRIVER WORKDIR
"""

import json
import os
import shutil
import sys
from pathlib import Path

from harness import Driven, data


def main() -> None:
    spec, driver, work = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    os.environ.setdefault("HOME", str(work))
    kernel = Path(json.loads(spec.read_text())["machines"][0]["launch"]["config"]["kernel"])
    copy = work / "linux"
    shutil.copyfile(kernel, copy)
    copy.chmod(0o755)

    good = Driven(driver, work / "good", "--kernel", str(copy))
    good.wait("with a copied kernel")
    if not any(e["kind"] == "note" and data(e).get("kernel") == str(copy) for e in good.events()):
        good.fail("the run did not record the override")
    print("ok: a kernel from outside the store booted, and the run says so")

    bogus = work / "bogus"
    bogus.write_text("not a kernel\n")
    bogus.chmod(0o755)
    bad = Driven(driver, work / "bad", "--kernel", str(bogus))
    if bad.exited() == 0:
        bad.fail("a run whose kernel is not a kernel passed: the override is ignored")
    print("ok: and a path that is not a kernel fails the boot")


if __name__ == "__main__":
    main()
