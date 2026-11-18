"""Does every check run in CI or say why it does not?

A check is a leaf of `checks`: `checks.lan`, or `checks.nixos-tests.simple-vm`
for a check that is a set of tests. A CI attribute covers a leaf when one
names the other or a part of it: `checks.lan.qemu` covers `lan`, and
`checks.nixos-tests` would cover each of its tests. A `byHand` key covers
leaves the same way.

The check fails on a leaf that nothing covers, on a `byHand` key that CI
covers too, and on a `byHand` key that covers no leaf.

    ci_set SET_JSON
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TypedDict

CHECKS = "checks."


class CiSet(TypedDict):
    """What `tests/ci-set/default.nix` writes."""

    leaves: list[str]
    attrs: list[str]
    byHand: dict[str, str]


def covers(name: str, leaf: str) -> bool:
    return name == leaf or name.startswith(f"{leaf}.") or leaf.startswith(f"{name}.")


def problems(ci_set: CiSet) -> list[str]:
    leaves = ci_set["leaves"]
    in_ci = [attr.removeprefix(CHECKS) for attr in ci_set["attrs"] if attr.startswith(CHECKS)]
    by_hand = ci_set["byHand"]
    found: list[str] = []
    for leaf in leaves:
        if not any(covers(name, leaf) for name in [*in_ci, *by_hand]):
            found.append(f"checks.{leaf} runs in no CI job; add it to one, or to `byHand` with the reason")
    for name in by_hand:
        if any(covers(name, attr) for attr in in_ci):
            found.append(f"byHand.{name} runs in CI too; drop it from `byHand`")
        if not any(covers(name, leaf) for leaf in leaves):
            found.append(f"byHand.{name} names no check; drop it from `byHand`")
    return found


def main() -> None:
    ci_set: CiSet = json.loads(Path(sys.argv[1]).read_text())
    found = problems(ci_set)
    if found:
        sys.exit("\n".join(["ci/workflows.nix and `checks` disagree:", *found]))


if __name__ == "__main__":
    main()
