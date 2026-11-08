"""`pynix-review diff`: the review bundle of two nixpkgs trees.

With `--out DIR` it writes `bundle.json`, `bundle.schema.json`,
`changed.dot` and `changed.url` there, and prints the paths and the view
link as JSON. Without it, it prints `bundle.json`.

`pynix-review plan BUNDLE`: what to build within a build-time budget.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import anyio
import huggorm

from . import bundle
from .buildtimes import UNREPORTED_PHASE_SECONDS
from .plan import BUDGET_SECONDS, UNTIMED_SECONDS, plan
from .review import diff


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pynix-review", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    cmd = commands.add_parser("diff", help="what a nixpkgs change rebuilds, layered by depth")
    cmd.add_argument("--before", required=True, help="nixpkgs directory or tarball URL")
    cmd.add_argument("--after", required=True, help="nixpkgs directory or tarball URL")
    cmd.add_argument("--attrs", type=Path, help="file with one attribute path per line")
    cmd.add_argument("--attr", action="append", default=[])
    cmd.add_argument("--system", default=huggorm.current_system())
    cmd.add_argument("--after-overlay", help="a Nix overlay applied to the after tree only")
    cmd.add_argument("--build-times", action="store_true", help="time changed derivations from cache.nixos.org logs")
    cmd.add_argument("--hydra", action="store_true", help="with --build-times, ask hydra.nixos.org for exact times")
    cmd.add_argument(
        "--phase-seconds",
        type=float,
        default=UNREPORTED_PHASE_SECONDS,
        help="seconds assumed for a logged phase that printed no footer (under 30 s)",
    )
    cmd.add_argument("--pool-size", type=int, default=4, help="huggorm's thread pool, for the closure walk")
    cmd.add_argument("--out", type=Path, help="write the bundle's files into this directory")

    cmd = commands.add_parser("plan", help="what to build from a bundle, within a build-time budget")
    cmd.add_argument("bundle", type=Path, help=f"a {bundle.BUNDLE_FILE}")
    cmd.add_argument("--budget", type=int, default=BUDGET_SECONDS, help="build seconds")
    cmd.add_argument(
        "--untimed-seconds",
        type=int,
        default=UNTIMED_SECONDS,
        help="seconds assumed for a derivation with no time",
    )
    return parser


async def _plan(args: argparse.Namespace) -> str:
    made = bundle.Bundle.model_validate_json(await anyio.Path(args.bundle).read_text())
    return plan(made, budget=args.budget, untimed_seconds=args.untimed_seconds).model_dump_json(indent=2)


async def _run(args: argparse.Namespace, attrs: list[str]) -> str:
    result = await diff(
        args.before,
        args.after,
        attrs,
        system=args.system,
        after_overlay=args.after_overlay,
        build_times=args.build_times,
        hydra=args.hydra,
        per_phase=args.phase_seconds,
    )
    made = bundle.from_diff(result, before=args.before, after=args.after, system=args.system, timed=args.build_times)
    if args.out is None:
        return made.model_dump_json(indent=2)
    written = await bundle.write(made, anyio.Path(args.out))
    return json.dumps({"files": [str(p) for p in written], "view": bundle.view_url(bundle.to_dot(made))}, indent=2)


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if args.command == "plan":
        sys.stdout.write(anyio.run(_plan, args) + "\n")
        return
    huggorm.set_pool_size(args.pool_size)

    attrs: list[str] = list(args.attr)
    if args.attrs:
        attrs += [ln.strip() for ln in args.attrs.read_text().splitlines() if ln.strip()]
    if not attrs:
        parser.error("give --attr or --attrs")

    sys.stdout.write(anyio.run(_run, args, attrs) + "\n")
