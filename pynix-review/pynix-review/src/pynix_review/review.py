"""Diff two nixpkgs trees: both evaluated at once, the change layered, and optionally timed."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anyio

from . import graph
from .buildtimes import Timing, fetch
from .evaluate import Side, evaluate

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(slots=True)
class Diff:
    before: Side
    after: Side
    changed: set[str]
    layers: list[set[str]]
    nearest: dict[str, int]
    twins: dict[str, str] = field(default_factory=dict)
    timings: dict[str, Timing] = field(default_factory=dict)
    seconds: dict[str, float] = field(default_factory=dict)

    def nearest_builds(self) -> list[set[str]]:
        """For each nearest depth k, what building depth <= k builds."""
        top = max(self.nearest.values(), default=0)
        return [
            graph.build_set(self.after.graph, self.changed, (d for d, n in self.nearest.items() if n <= k))
            for k in range(1, top + 1)
        ]


async def diff(
    before: str,
    after: str,
    attrs: Sequence[str],
    *,
    system: str,
    after_overlay: str | None = None,
    build_times: bool = False,
    hydra: bool = False,
) -> Diff:
    """What `after` changes against `before`, for the closure of `attrs`.

    `before` and `after` are nixpkgs directories or tarball URLs."""
    start = time.monotonic()
    sides: dict[str, Side] = {}

    async def one(name: str, tree: str, overlay: str | None) -> None:
        sides[name] = await evaluate(tree, attrs, system=system, overlay=overlay)

    async with anyio.create_task_group() as tg:
        tg.start_soon(one, "before", before, None)
        tg.start_soon(one, "after", after, after_overlay)
    eval_wall = time.monotonic() - start

    old, new = sides["before"], sides["after"]
    changed = graph.changed(old.graph, new.graph)
    result = Diff(
        before=old,
        after=new,
        changed=changed,
        layers=graph.layers(new.graph, changed),
        nearest=graph.nearest(new.graph, changed),
        seconds={
            "before": old.seconds,
            "after": new.seconds,
            "before_walk": old.walk_seconds,
            "after_walk": new.walk_seconds,
            "evaluate_wall": eval_wall,
        },
    )
    if build_times:
        start = time.monotonic()
        pairs = [(a, old.roots[attr]) for attr, a in new.roots.items() if attr in old.roots]
        result.twins = graph.twins(old.graph, new.graph, pairs, changed)
        # The after .drv first: for a merged change Hydra has built it.
        candidates = {d: [d, *([result.twins[d]] if d in result.twins else [])] for d in changed}
        jobs = {a: f"{attr}.{system}" for attr, a in new.roots.items() if a in changed}
        result.timings = await fetch(candidates, jobs, hydra=hydra)
        result.seconds["build_times"] = time.monotonic() - start
    return result
