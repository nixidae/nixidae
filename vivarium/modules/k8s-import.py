"""vivarium-k8s-import TARBALL...: images into this node's container runtime.

One command for the boot unit (`k8s-load-images`) and for `vivarium load`,
so the runtime's import lives in one place. stdout is the runtime's image
list after the imports; progress goes to stderr.

A tarball may be gzipped or not; this reads its first two bytes. `ctr
images import` does not, and reports `invalid tar header` on a gzipped
tarball such as `dockerTools.mergeImages` writes.

Piped rather than named: `--discard-unpacked-layers`, which drops the
compressed copy once the snapshotter has it, is only on the `--local`
path, which imports here rather than through containerd's transfer
service. CRI-O reads containers-storage, which is podman's; `podman load`
takes a multi-image archive where `skopeo copy` wants one per tag.
"""

from __future__ import annotations

import gzip
import json
import shutil
import subprocess
import sys
from pathlib import Path

IMPORT = json.loads("""@import@""")
LIST = json.loads("""@list@""")
GZIP = b"\x1f\x8b"
CHUNK = 1 << 20


def load(path: Path) -> None:
    sys.stderr.write(f"importing {path}\n")
    with path.open("rb") as raw:
        gzipped = raw.read(2) == GZIP
        raw.seek(0)
        source = gzip.GzipFile(fileobj=raw) if gzipped else raw
        # The runtime's own progress is not the result: to stderr.
        with subprocess.Popen(IMPORT, stdin=subprocess.PIPE, stdout=sys.stderr) as runtime:
            if runtime.stdin is None:
                sys.exit("no pipe to the runtime")
            shutil.copyfileobj(source, runtime.stdin, CHUNK)
            runtime.stdin.close()
            if runtime.wait() != 0:
                sys.exit(f"{IMPORT[0]} exited {runtime.returncode} on {path}")


def main() -> None:
    for name in sys.argv[1:]:
        load(Path(name))
    subprocess.run(LIST, check=True)


if __name__ == "__main__":
    main()
