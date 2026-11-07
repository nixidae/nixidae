"""`pynix-review diff`: the report of `review.diff`, as JSON on stdout."""

from __future__ import annotations

import argparse
import json
import resource
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import huggorm

from .buildtimes import Source, Timing
from .review import Diff, diff

if TYPE_CHECKING:
    from collections.abc import Iterable

# A report is JSON of fixed shape built from dataclasses; the values are
# str, int, float, list, dict and None.
type Json = object


def _maxrss_mib() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024


def time_summary(drvs: Iterable[str], timings: dict[str, Timing]) -> dict[str, int]:
    drvs = list(drvs)
    timed = [timings[d] for d in drvs if d in timings]
    return {
        "seconds": sum(t.seconds for t in timed),
        **{str(s): sum(1 for t in timed if t.source is s) for s in Source},
        "untimed": len(drvs) - len(timed),
    }


def report(result: Diff, attrs: list[str], *, show: int, timed: bool) -> dict[str, Json]:
    depth_of = {d: n for n, layer in enumerate(result.layers, 1) for d in layer}

    def times(drvs: Iterable[str]) -> dict[str, Json]:
        return {"time": time_summary(drvs, result.timings)} if timed else {}

    before, after = result.before, result.after
    return {
        "attrs": {
            attr: {
                "before": before.roots.get(attr),
                "after": after.roots.get(attr),
                "depth": depth_of.get(after.roots.get(attr, "")),
                "nearest": result.nearest.get(after.roots.get(attr, "")),
            }
            for attr in attrs
        },
        "failed": {"before": before.failed, "after": after.failed},
        "derivations": {"before": len(before.graph), "after": len(after.graph)},
        "changed": len(result.changed),
        "layers": [
            {"depth": n, "count": len(layer), **times(layer), "sample": sorted(layer)[:show]}
            for n, layer in enumerate(result.layers, 1)
        ],
        "nearest": [
            {
                "depth": k,
                "selected": sum(1 for n in result.nearest.values() if n <= k),
                "builds": len(builds),
                **times(builds),
            }
            for k, builds in enumerate(result.nearest_builds(), 1)
        ],
        "build_times": {
            **times(result.changed),
            "matched_before": len(result.twins),
            "slowest": [
                {"drv": d, "seconds": t.seconds, "source": str(t.source), "from": t.drv}
                for d, t in sorted(result.timings.items(), key=lambda kv: -kv[1].seconds)[:show]
            ],
            "untimed": sorted(result.changed - set(result.timings))[:show],
        }
        if timed
        else None,
        "seconds": {k: round(v, 2) for k, v in result.seconds.items()},
        "maxrss_mib": _maxrss_mib(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="pynix-review", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    cmd = commands.add_parser("diff", help="what a nixpkgs change rebuilds, layered by depth")
    cmd.add_argument("--before", required=True, help="nixpkgs directory or tarball URL")
    cmd.add_argument("--after", required=True, help="nixpkgs directory or tarball URL")
    cmd.add_argument("--attrs", type=Path, help="file with one attribute path per line")
    cmd.add_argument("--attr", action="append", default=[])
    cmd.add_argument("--system", default=huggorm.current_system())
    cmd.add_argument("--after-overlay", help="a Nix overlay applied to the after tree only")
    cmd.add_argument("--show", type=int, default=20, help="derivations listed per layer")
    cmd.add_argument("--build-times", action="store_true", help="time changed derivations from cache.nixos.org logs")
    cmd.add_argument("--hydra", action="store_true", help="with --build-times, ask hydra.nixos.org for exact times")
    cmd.add_argument("--pool-size", type=int, default=4, help="huggorm's thread pool, for the closure walk")
    args = parser.parse_args()
    huggorm.set_pool_size(args.pool_size)

    attrs: list[str] = list(args.attr)
    if args.attrs:
        attrs += [ln.strip() for ln in args.attrs.read_text().splitlines() if ln.strip()]
    if not attrs:
        parser.error("give --attr or --attrs")

    async def run() -> Diff:
        return await diff(
            args.before,
            args.after,
            attrs,
            system=args.system,
            after_overlay=args.after_overlay,
            build_times=args.build_times,
            hydra=args.hydra,
        )

    result = anyio.run(run)
    json.dump(report(result, attrs, show=args.show, timed=args.build_times), sys.stdout, indent=2)
    sys.stdout.write("\n")
