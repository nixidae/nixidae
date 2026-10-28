"""That every mypy.ini is strict and checks at its project's Python floor.

Usage: mypy-ini.py [--floor=SOURCE/mypy.ini=X.Y]... NAME=PATH...

The floor is `requires-python` in the nearest pyproject.toml at or above
the mypy.ini; `--floor` names it for a source with no pyproject.toml.
A mypy.ini with none above it (huggorm, nanopynix) covers several
projects in one run, and mypy sets `python_version` once per run, so it
takes the highest floor below it: the lowest version that parses all of
them. nixidae/nixidae#1113.
"""

from __future__ import annotations

import configparser
import re
import sys
import tomllib
from pathlib import Path

_FLOOR = re.compile(r">=\s*(\d+\.\d+)")


def floor(pyproject: Path) -> tuple[int, int] | None:
    spec = (
        tomllib.loads(pyproject.read_text()).get("project", {}).get("requires-python")
    )
    if not isinstance(spec, str):
        return None
    match = _FLOOR.search(spec)
    if match is None:
        return None
    major, minor = match.group(1).split(".")
    return int(major), int(minor)


def expected(root: Path, ini: Path) -> tuple[int, int] | None:
    for directory in ini.parents:
        candidate = directory / "pyproject.toml"
        if candidate.is_file() and (found := floor(candidate)) is not None:
            return found
        if directory == root:
            break
    floors = [
        f for p in ini.parent.rglob("pyproject.toml") if (f := floor(p)) is not None
    ]
    return max(floors, default=None)


def problems(name: str, root: Path, floors: dict[str, str]) -> list[str]:
    found: list[str] = []
    for ini in sorted(root.rglob("mypy.ini")):
        label = f"{name}/{ini.relative_to(root)}"
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(ini)
        if not parser.has_section("mypy"):
            found.append(f"{label}: no [mypy] section")
            continue
        section = parser["mypy"]
        if section.get("strict") != "True":
            found.append(f"{label}: strict = True is missing")
        if label in floors:
            want = floors[label]
        elif (version := expected(root, ini)) is not None:
            want = f"{version[0]}.{version[1]}"
        else:
            found.append(f"{label}: no requires-python floor found")
            continue
        have = section.get("python_version")
        if have != want:
            found.append(f"{label}: python_version = {have}, the floor is {want}")
    return found


def main(argv: list[str]) -> int:
    floors = dict(
        arg.removeprefix("--floor=").split("=", 1)
        for arg in argv
        if arg.startswith("--floor=")
    )
    found: list[str] = []
    for arg in argv:
        if arg.startswith("--floor="):
            continue
        name, _, path = arg.partition("=")
        found += problems(name, Path(path), floors)
    for line in found:
        print(line, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
