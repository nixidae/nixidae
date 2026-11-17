"""typecheck.py OUT CONFIG PYTHON SCRIPT...: `vivarium.typeCheck`'s builder.

One zuban run per script: two scripts may share a basename --
`recipes/boot.py` and `tests/phases/boot.py` do -- and one run refuses two
modules of the same name.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    out, config, python, *scripts = argv
    failed = [
        script
        for script in scripts
        if subprocess.run(
            ["zuban", "mypy", "--config-file", config, "--python-executable", python, script], check=False
        ).returncode
        != 0
    ]
    if failed:
        print(f"{len(failed)} of the scripts above do not type check against vivarium_runner", file=sys.stderr)
        return 1
    Path(out).touch()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
