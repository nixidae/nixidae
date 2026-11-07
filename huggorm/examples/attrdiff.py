"""Which derivations a nixpkgs change rebuilds, layered by depth.

Evaluates the same attributes in two nixpkgs trees, each in its own
`EvalState` over its own in-memory `dummy://` store, so no `.drv` reaches
disk. The changed set is every derivation in the after-closure that the
before-closure lacks. Depth 1 is the changed derivations with no changed
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
import resource
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--before", required=True, help="nixpkgs directory or tarball URL")
    parser.add_argument("--after", required=True, help="nixpkgs directory or tarball URL")
    parser.add_argument("--attrs", type=Path, help="file with one attribute path per line")
    parser.add_argument("--attr", action="append", default=[])
    parser.add_argument("--system", default=huggorm.current_system())
    parser.add_argument("--after-overlay", help="a Nix overlay applied to the after tree only")
    parser.add_argument("--show", type=int, default=20, help="derivations listed per layer")
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

    changed = set(after.inputs) - set(before.inputs)
    start = time.monotonic()
    found = layers(changed, after.inputs)
    near = nearest(changed, after.inputs)
    by_near = [
        build_set({d for d, n in near.items() if n <= k}, changed, after.inputs)
        for k in range(1, max(near.values(), default=0) + 1)
    ]
    layer_seconds = time.monotonic() - start
    depth_of = {d: n for n, layer in enumerate(found, 1) for d in layer}

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
            {"depth": n, "count": len(layer), "sample": sorted(layer)[: args.show]} for n, layer in enumerate(found, 1)
        ],
        "nearest": [
            {
                "depth": k,
                "selected": sum(1 for n in near.values() if n <= k),
                "builds": len(builds),
            }
            for k, builds in enumerate(by_near, 1)
        ],
        "seconds": {
            "before": round(before.seconds, 2),
            "after": round(after.seconds, 2),
            "layers": round(layer_seconds, 3),
        },
        "maxrss_mib": {"after_before_side": rss_before, "both_sides": rss_both},
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
