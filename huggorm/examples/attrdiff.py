"""Which derivations a nixpkgs change rebuilds, layered by depth.

Evaluates the same attributes in two nixpkgs trees, each in its own
`EvalState` over its own in-memory `dummy://` store, so no `.drv` reaches
disk. The changed set is every derivation in the after-closure that the
before-closure lacks. A fixed-output derivation matches by name and
content address, not by `.drv` path: a Go `goModules` whose inputs moved
has a new `.drv` and the same output, and builds nothing. Depth 1 is the changed derivations with no changed
input; depth n+1 is what depth n leaves with no remaining changed input
(the design in Mic92/nixpkgs-review#554).

A derivation that exists unchanged in the before tree but outside the
before-closure of the attributes given counts as changed. Pass enough
attributes, or the dependents of a renamed package land at depth 1.

Prints JSON to stdout.
"""

from __future__ import annotations

import argparse
import json
import re
import resource
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import anyio
from buildtimes import Source, Timing, fetch

import huggorm
from huggorm import errors


@dataclass(slots=True)
class Side:
    store: huggorm.Store
    state: huggorm.EvalState
    pkgs: huggorm.Value
    roots: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    inputs: dict[str, frozenset[str]] = field(default_factory=dict)
    identity: dict[str, str] = field(default_factory=dict)
    seconds: float = 0.0


def _maxrss_mib() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024


def tree_expr(tree: str) -> str:
    """A Nix expression for a nixpkgs tree: a directory, or a tarball URL.

    The state that evaluates fetches a URL itself. A tree that another
    process fetched is valid in the host store only, and newer Nix keeps
    it lazily in its git cache, not on disk, so the dummy store cannot
    read it."""
    if tree.startswith(("https://", "http://")):
        return f"(builtins.fetchTarball {json.dumps(tree)})"
    return str(Path(tree).resolve())


def open_side(tree: str, system: str, overlay: str | None) -> Side:
    store = huggorm.Store("dummy://?read-only=false")
    state = huggorm.EvalState(
        store,
        settings={"allow-import-from-derivation": "false", "pure-eval": "false"},
    )
    overlays = f"[ ({overlay}) ]" if overlay else "[ ]"
    # The same config CI's outpaths.nix passes, minus what only matters there.
    expr = (
        f"import {tree_expr(tree)} {{ system = {json.dumps(system)}; overlays = {overlays}; "
        "config = { allowAliases = false; allowUnfree = true; "
        "allowInsecurePredicate = _: true; }; }"
    )
    pkgs = state.eval_expr(expr)
    state.force(pkgs)
    return Side(store=store, state=state, pkgs=pkgs)


def resolve(side: Side, attr: str) -> huggorm.Value:
    value = side.pkgs
    for name in attr.split("."):
        value = value.get(name)
        side.state.force(value)
    return value


def identity(base: str, parsed: huggorm.Derivation) -> str:
    """What decides whether this derivation builds something new."""
    outputs = parsed.outputs()
    fixed = [o for o in outputs.values() if isinstance(o, huggorm.DerivationOutputCAFixed)]
    if fixed and len(fixed) == len(outputs):
        return f"fixed:{parsed.name()}:" + ",".join(sorted(str(o.ca()) for o in fixed))
    return base


def walk(side: Side, drv: str) -> None:
    """Record the input edges of every derivation under `drv` not yet seen."""
    queue = deque([drv])
    while queue:
        base = queue.popleft()
        if base in side.inputs:
            continue
        parsed = side.store.read_derivation(huggorm.StorePath(base))
        edges = frozenset(parsed.input_drvs())
        side.inputs[base] = edges
        side.identity[base] = identity(base, parsed)
        queue.extend(e for e in edges if e not in side.inputs)


def evaluate(side: Side, attrs: list[str]) -> None:
    start = time.monotonic()
    for attr in attrs:
        try:
            drv = resolve(side, attr).drv_path()
        except errors.EvalBaseError as e:
            side.failed[attr] = str(e).splitlines()[0][:300]
            continue
        base = drv.to_string()
        side.roots[attr] = base
        walk(side, base)
    side.seconds = time.monotonic() - start


def layers(changed: set[str], inputs: dict[str, frozenset[str]]) -> list[set[str]]:
    remaining = set(changed)
    out: list[set[str]] = []
    while remaining:
        layer = {d for d in remaining if not (inputs[d] & remaining)}
        if not layer:
            msg = "derivation graph has a cycle"
            raise RuntimeError(msg)
        out.append(layer)
        remaining -= layer
    return out


def nearest(changed: set[str], inputs: dict[str, frozenset[str]]) -> dict[str, int]:
    """Shortest changed-edge distance from a changed derivation with no changed input.

    `layers` measures the longest path: cairo depends on libpng directly but
    sits at depth 8, behind fontconfig. "Everything that directly depends on
    what I touched" is this distance instead."""
    dependents: dict[str, set[str]] = {d: set() for d in changed}
    for d in changed:
        for i in inputs[d] & changed:
            dependents[i].add(d)
    dist = {d: 1 for d in changed if not (inputs[d] & changed)}
    queue = deque(dist)
    while queue:
        d = queue.popleft()
        for up in dependents[d]:
            if up not in dist:
                dist[up] = dist[d] + 1
                queue.append(up)
    return dist


def build_set(selected: set[str], changed: set[str], inputs: dict[str, frozenset[str]]) -> set[str]:
    """`selected` plus every changed derivation it needs to build."""
    out: set[str] = set()
    queue = deque(selected)
    while queue:
        d = queue.popleft()
        if d in out:
            continue
        out.add(d)
        queue.extend(inputs[d] & changed)
    return out


def pname(drv: str) -> str:
    """`builtins.parseDrvName`'s name part of a `.drv` base name."""
    name = drv.removesuffix(".drv").split("-", 1)[1]
    match = re.search(r"-[^a-zA-Z]", name)
    return name[: match.start()] if match else name


def twins(before: Side, after: Side, changed: set[str]) -> dict[str, str]:
    """The before derivation each changed after derivation replaces.

    Matched from the attribute roots down, an input to an input with the
    same name: a name alone is ambiguous, since many fetches are `source`.
    A version bump changes the name's version, not its `pname`."""
    out: dict[str, str] = {}
    queue = deque((a, before.roots[attr]) for attr, a in after.roots.items() if a in changed and attr in before.roots)
    while queue:
        a, b = queue.popleft()
        if a in out:
            continue
        out[a] = b
        for ia in after.inputs[a] & changed:
            if ia in out:
                continue
            same = [ib for ib in before.inputs[b] if pname(ib) == pname(ia)]
            if len(same) == 1:
                queue.append((ia, same[0]))
    return out


def time_summary(drvs: set[str], timings: dict[str, Timing]) -> dict[str, int]:
    timed = [timings[d] for d in drvs if d in timings]
    return {
        "seconds": sum(t.seconds for t in timed),
        **{str(s): sum(1 for t in timed if t.source is s) for s in Source},
        "untimed": len(drvs) - len(timed),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--before", required=True, help="nixpkgs directory or tarball URL")
    parser.add_argument("--after", required=True, help="nixpkgs directory or tarball URL")
    parser.add_argument("--attrs", type=Path, help="file with one attribute path per line")
    parser.add_argument("--attr", action="append", default=[])
    parser.add_argument("--system", default=huggorm.current_system())
    parser.add_argument("--after-overlay", help="a Nix overlay applied to the after tree only")
    parser.add_argument("--show", type=int, default=20, help="derivations listed per layer")
    parser.add_argument(
        "--build-times",
        action="store_true",
        help="time each changed derivation from cache.nixos.org build logs",
    )
    parser.add_argument(
        "--hydra",
        action="store_true",
        help="with --build-times, ask hydra.nixos.org for the exact time of attribute roots",
    )
    args = parser.parse_args()

    attrs: list[str] = list(args.attr)
    if args.attrs:
        attrs += [ln.strip() for ln in args.attrs.read_text().splitlines() if ln.strip()]
    if not attrs:
        parser.error("give --attr or --attrs")

    before = open_side(args.before, args.system, None)
    evaluate(before, attrs)
    rss_before = _maxrss_mib()
    after = open_side(args.after, args.system, args.after_overlay)
    evaluate(after, attrs)
    rss_both = _maxrss_mib()

    known = set(before.identity.values())
    changed = {d for d, ident in after.identity.items() if ident not in known}
    start = time.monotonic()
    found = layers(changed, after.inputs)
    near = nearest(changed, after.inputs)
    by_near = [
        build_set({d for d, n in near.items() if n <= k}, changed, after.inputs)
        for k in range(1, max(near.values(), default=0) + 1)
    ]
    layer_seconds = time.monotonic() - start
    depth_of = {d: n for n, layer in enumerate(found, 1) for d in layer}

    timings: dict[str, Timing] = {}
    twin: dict[str, str] = {}
    fetch_seconds = 0.0
    if args.build_times:
        start = time.monotonic()
        twin = twins(before, after, changed)
        # The after .drv first: for a merged change Hydra has built it, and
        # its own log beats the one it replaces.
        candidates = {d: [d, *([twin[d]] if d in twin else [])] for d in changed}
        jobs = {a: f"{attr}.{args.system}" for attr, a in after.roots.items() if a in changed}
        timings = anyio.run(lambda: fetch(candidates, jobs, hydra=args.hydra))
        fetch_seconds = time.monotonic() - start

    def timed(drvs: set[str]) -> dict[str, dict[str, int]]:
        return {"time": time_summary(drvs, timings)} if args.build_times else {}

    report = {
        "system": args.system,
        "attrs": {
            attr: {
                "before": before.roots.get(attr),
                "after": after.roots.get(attr),
                "depth": depth_of.get(after.roots.get(attr, "")),
                "nearest": near.get(after.roots.get(attr, "")),
            }
            for attr in attrs
        },
        "failed": {"before": before.failed, "after": after.failed},
        "derivations": {"before": len(before.inputs), "after": len(after.inputs)},
        "changed": len(changed),
        "layers": [
            {"depth": n, "count": len(layer), **timed(layer), "sample": sorted(layer)[: args.show]}
            for n, layer in enumerate(found, 1)
        ],
        "nearest": [
            {
                "depth": k,
                "selected": sum(1 for n in near.values() if n <= k),
                "builds": len(builds),
                **timed(builds),
            }
            for k, builds in enumerate(by_near, 1)
        ],
        "build_times": {
            **timed(changed),
            "matched_before": len(twin),
            "slowest": [
                {"drv": d, "seconds": t.seconds, "source": t.source, "from": t.drv}
                for d, t in sorted(timings.items(), key=lambda kv: -kv[1].seconds)[: args.show]
            ],
            "untimed": sorted(changed - set(timings))[: args.show],
        }
        if args.build_times
        else None,
        "seconds": {
            "before": round(before.seconds, 2),
            "after": round(after.seconds, 2),
            "layers": round(layer_seconds, 3),
            "build_times": round(fetch_seconds, 2),
        },
        "maxrss_mib": {"after_before_side": rss_before, "both_sides": rss_both},
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
