"""The derivation graph of a nixpkgs change: what changed, and in what order it builds.

Pure: every function takes the graph as data and returns a value.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

_VERSION_START = re.compile(r"-[^a-zA-Z]")


@dataclass(frozen=True, slots=True)
class Drv:
    """One derivation, as the graph needs it.

    `base` is the `.drv` base name, `<hash>-<name>.drv`. `fixed` holds the
    content address of each output when every output is fixed, else None."""

    base: str
    name: str
    inputs: frozenset[str]
    fixed: tuple[str, ...] | None = None


Graph = dict[str, Drv]


def pname(base: str) -> str:
    """`builtins.parseDrvName`'s name part of a `.drv` base name."""
    name = base.removesuffix(".drv").split("-", 1)[1]
    match = _VERSION_START.search(name)
    return name[: match.start()] if match else name


def identity(drv: Drv) -> str:
    """What decides whether a derivation builds something new.

    A fixed-output derivation is its name and content address, not its
    `.drv` path: a Go `goModules` whose inputs moved gets a new `.drv` and
    the same output, and builds nothing."""
    if drv.fixed is not None:
        return f"fixed:{drv.name}:" + ",".join(sorted(drv.fixed))
    return drv.base


def changed(before: Mapping[str, Drv], after: Mapping[str, Drv]) -> set[str]:
    """Every derivation of `after` that `before` holds no equal of.

    A derivation that is unchanged in the before tree, but outside the
    before closure that was walked, counts as changed."""
    known = {identity(d) for d in before.values()}
    return {base for base, d in after.items() if identity(d) not in known}


def _inputs(graph: Mapping[str, Drv], base: str, within: set[str]) -> frozenset[str]:
    return graph[base].inputs & within


def layers(graph: Mapping[str, Drv], changed: set[str]) -> list[set[str]]:
    """The changed set by longest path: depth 1 has no changed input.

    The rule of Mic92/nixpkgs-review#554. Depth k holds every changed input
    of depth k at a smaller depth, so building depth <= k builds exactly
    what it selects."""
    remaining = set(changed)
    out: list[set[str]] = []
    while remaining:
        layer = {d for d in remaining if not _inputs(graph, d, remaining)}
        if not layer:
            msg = "derivation graph has a cycle"
            raise ValueError(msg)
        out.append(layer)
        remaining -= layer
    return out


def nearest(graph: Mapping[str, Drv], changed: set[str]) -> dict[str, int]:
    """Shortest changed-edge distance from a changed derivation with no changed input.

    "Everything that directly depends on what I touched" is this distance.
    After a libpng change cairo is at 2 here and at 8 by `layers`, behind
    fontconfig. Building depth <= k here needs `build_set`."""
    dependents: dict[str, set[str]] = {d: set() for d in changed}
    for d in changed:
        for i in _inputs(graph, d, changed):
            dependents[i].add(d)
    dist = {d: 1 for d in changed if not _inputs(graph, d, changed)}
    queue = deque(dist)
    while queue:
        d = queue.popleft()
        for up in dependents[d]:
            if up not in dist:
                dist[up] = dist[d] + 1
                queue.append(up)
    return dist


def build_set(graph: Mapping[str, Drv], changed: set[str], selected: Iterable[str]) -> set[str]:
    """`selected` plus every changed derivation it needs to build."""
    out: set[str] = set()
    queue = deque(selected)
    while queue:
        d = queue.popleft()
        if d in out:
            continue
        out.add(d)
        queue.extend(_inputs(graph, d, changed))
    return out


def twins(
    before: Mapping[str, Drv],
    after: Mapping[str, Drv],
    roots: Iterable[tuple[str, str]],
    changed: set[str],
) -> dict[str, str]:
    """The before derivation each changed after derivation replaces.

    `roots` pairs an after root with its before root, by attribute. From
    there down, an input matches the input of the same `pname`: a name alone
    is ambiguous, since many fetches are `source`. A version bump changes the
    version, not the `pname`. Two candidates match neither."""
    out: dict[str, str] = {}
    queue = deque((a, b) for a, b in roots if a in changed)
    while queue:
        a, b = queue.popleft()
        if a in out:
            continue
        out[a] = b
        for ia in _inputs(after, a, changed):
            if ia in out:
                continue
            same = [ib for ib in before[b].inputs if pname(ib) == pname(ia)]
            if len(same) == 1:
                queue.append((ia, same[0]))
    return out
