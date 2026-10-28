"""Write a venv's console and GUI scripts from its packages' entry points.

Usage: entry-points.py SITE_PACKAGES BIN_DIR SHEBANG_PYTHON

Each script names the venv's python in its shebang, so it starts inside the
venv the way pip-installed scripts do. Two distributions declaring one name
with different targets is an error, not a silent last-wins.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib.metadata import distributions
from pathlib import Path
from typing import Final

GROUPS: Final = ("console_scripts", "gui_scripts")


@dataclass(frozen=True, slots=True)
class Claim:
    """The distribution that declares a script, and the target it names."""

    owner: str
    target: str


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
    claims: dict[str, Claim] = {}
    for dist in distributions(path=[site]):
        for ep in dist.entry_points:
            if ep.group not in GROUPS:
                continue
            name = ep.name
            owner = dist.metadata["Name"]
            if (claim := claims.get(name)) is not None and claim.target != ep.value:
                raise SystemExit(
                    f"entry point {name!r}: {claim.owner} names "
                    f"{claim.target!r}, {owner} names {ep.value!r}"
                )
            claims[name] = Claim(owner, ep.value)
            if not ep.attr:
                raise SystemExit(
                    f"entry point {name!r} ({owner}) names no attribute: {ep.value!r}"
                )
            path = Path(bin_dir) / name
            path.write_text(script(shebang, ep.module, ep.attr))
            path.chmod(0o555)


if __name__ == "__main__":
    main()
