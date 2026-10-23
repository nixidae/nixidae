"""Write a venv's console and GUI scripts from its packages' entry points.

Usage: entry-points.py SITE_PACKAGES BIN_DIR SHEBANG_PYTHON

Each script names the venv's python in its shebang, so it starts inside the
venv the way pip-installed scripts do. Two distributions declaring one name
with different targets is an error, not a silent last-wins.
"""

from __future__ import annotations

import sys
from importlib.metadata import distributions
from pathlib import Path
from typing import Final

GROUPS: Final = ("console_scripts", "gui_scripts")


def script(shebang: str, module: str, attr: str) -> str:
    head = attr.partition(".")[0]
    return (
        f"#!{shebang}\n"
        "import sys\n"
        f"from {module} import {head}\n"
        'if __name__ == "__main__":\n'
        f"    sys.exit({attr}())\n"
    )


def main() -> None:
    site, bin_dir, shebang = sys.argv[1:]
    targets: dict[str, tuple[str, str]] = {}
    for dist in distributions(path=[site]):
        for ep in dist.entry_points:
            if ep.group not in GROUPS:
                continue
            name = ep.name
            owner = dist.metadata["Name"]
            if name in targets and targets[name][1] != ep.value:
                raise SystemExit(
                    f"entry point {name!r}: {targets[name][0]} names "
                    f"{targets[name][1]!r}, {owner} names {ep.value!r}"
                )
            targets[name] = (owner, ep.value)
            if not ep.attr:
                raise SystemExit(
                    f"entry point {name!r} ({owner}) names no attribute: {ep.value!r}"
                )
            path = Path(bin_dir) / name
            path.write_text(script(shebang, ep.module, ep.attr))
            path.chmod(0o555)


if __name__ == "__main__":
    main()
