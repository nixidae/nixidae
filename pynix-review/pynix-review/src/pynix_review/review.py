"""Diff two nixpkgs trees: both evaluated at once, the change layered, and optionally timed."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anyio

from . import graph
from .buildtimes import UNREPORTED_PHASE_SECONDS, Timing, fetch
from .evaluate import SCRATCH_STORE, Side, evaluate

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
    reference: Side | None = None
    references: dict[str, str] = field(default_factory=dict)
    """The reference tree's derivation for each changed one it has."""
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
    per_phase: float = UNREPORTED_PHASE_SECONDS,
    after_store: str = SCRATCH_STORE,
    reference: str | None = None,
) -> Diff:
    """What `after` changes against `before`, for the closure of `attrs`.

    `before` and `after` are nixpkgs directories or tarball URLs. The after
    side's `.drv` files go to `after_store`; `daemon` makes them buildable.

    With `build_times`, `reference` is a third tree that Hydra has built,
    such as the nixpkgs-unstable channel. A merge base younger than Hydra's
    backlog has no logs: on NixOS/nixpkgs#584702, 0 of 100 derivations had a
    time. The reference's build of the same package then stands in."""
    start = time.monotonic()
    sides: dict[str, Side] = {}

    async def one(name: str, tree: str, overlay: str | None, store_url: str) -> None:
        sides[name] = await evaluate(tree, attrs, system=system, overlay=overlay, store_url=store_url)

    async with anyio.create_task_group() as tg:
        tg.start_soon(one, "before", before, None, SCRATCH_STORE)
        tg.start_soon(one, "after", after, after_overlay, after_store)
        if build_times and reference is not None:
            tg.start_soon(one, "reference", reference, None, SCRATCH_STORE)
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
        if (ref := sides.get("reference")) is not None:
            result.reference = ref
            result.seconds["reference"] = ref.seconds
            ref_pairs = [(a, ref.roots[attr]) for attr, a in new.roots.items() if attr in ref.roots]
            result.references = graph.twins(ref.graph, new.graph, ref_pairs, changed)
        nearby = {d: [r] for d, r in result.references.items() if r not in candidates[d]}
        jobs = {a: f"{attr}.{system}" for attr, a in new.roots.items() if a in changed}
        graphs = [old.graph, new.graph, *([ref.graph] if ref is not None else [])]
        outs = {d.base: d.out for g in graphs for d in g.values() if d.out is not None}
        result.timings = await fetch(candidates, jobs, hydra=hydra, per_phase=per_phase, outs=outs, nearby=nearby)
        result.seconds["build_times"] = time.monotonic() - start
    return result
