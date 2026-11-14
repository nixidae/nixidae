"""`pynix-review diff`: the review bundle of two nixpkgs trees.

With `--out DIR` it writes `bundle.json`, `bundle.schema.json`,
`changed.dot` and `changed.url` there, and prints the paths and the view
link as JSON. Without it, it prints `bundle.json`.

`pynix-review plan BUNDLE`: what would build within a build-time budget, by the estimates.
`pynix-review build BUNDLE`: build what fits in a wall-clock budget, a result per derivation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import anyio
import huggorm

from . import bundle
from .build import DAEMON, DOT_FILE as BUILT_DOT, JOBS, WALL_SECONDS, build, write as write_built
from .buildtimes import UNREPORTED_PHASE_SECONDS
from .evaluate import SCRATCH_STORE
from .plan import BUDGET_SECONDS, UNTIMED_SECONDS, plan
from .review import diff
from .share import TOKEN_ENV, gist, url_view

CHANNEL = "https://channels.nixos.org/nixpkgs-unstable/nixexprs.tar.xz"
GIST_HELP = f"with --out, put the DOT in a secret gist and link to it ({TOKEN_ENV} needs the gist scope)"


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
    cmd.add_argument(
        "--after-store",
        default=SCRATCH_STORE,
        help="store for the after side's .drv files; `daemon` makes them buildable",
    )
    cmd.add_argument(
        "--reference",
        help=f"with --build-times, a tree Hydra has built, for times the merge base lacks; e.g. {CHANNEL}",
    )
    cmd.add_argument("--pool-size", type=int, default=4, help="huggorm's thread pool, for the closure walk")
    cmd.add_argument("--out", type=Path, help="write the bundle's files into this directory")
    cmd.add_argument("--gist", action="store_true", help=GIST_HELP)

    cmd = commands.add_parser("plan", help="what to build from a bundle, within a build-time budget")
    cmd.add_argument("bundle", type=Path, help=f"a {bundle.BUNDLE_FILE}")
    cmd.add_argument("--budget", type=int, default=BUDGET_SECONDS, help="build seconds")
    cmd.add_argument(
        "--untimed-seconds",
        type=int,
        default=UNTIMED_SECONDS,
        help="seconds assumed for a derivation with no time",
    )

    cmd = commands.add_parser("build", help="build what fits in a wall-clock budget, and report on each derivation")
    cmd.add_argument("bundle", type=Path, help=f"a {bundle.BUNDLE_FILE}")
    cmd.add_argument("--wall", type=int, default=WALL_SECONDS, help="seconds to start builds in; running ones finish")
    cmd.add_argument("--jobs", type=int, default=JOBS, help="builds at once")
    cmd.add_argument(
        "--untimed-seconds",
        type=int,
        default=UNTIMED_SECONDS,
        help="seconds assumed for a derivation with no time, for the order",
    )
    cmd.add_argument("--store", default=DAEMON, help="the store that builds; the daemon picks the machines")
    cmd.add_argument("--out", type=Path, help="write built.json, its schema, built.dot and built.url here")
    cmd.add_argument("--print-build-logs", action="store_true", help="each builder's output and Nix's info on stderr")
    cmd.add_argument("--gist", action="store_true", help=GIST_HELP)
    return parser


async def _build(args: argparse.Namespace) -> str:
    made = bundle.Bundle.model_validate_json(await anyio.Path(args.bundle).read_text())
    result = await build(
        made,
        wall=args.wall,
        jobs=args.jobs,
        untimed_seconds=args.untimed_seconds,
        store_url=args.store,
        build_logs=args.print_build_logs,
    )
    if args.out is None:
        return result.model_dump_json(indent=2)
    url = await _view(args, bundle.to_dot(made, result), BUILT_DOT, f"pynix-review build: {made.after}")
    written = await write_built(result, made, anyio.Path(args.out), url)
    return json.dumps({"files": [str(p) for p in written], "view": url}, indent=2)


async def _view(args: argparse.Namespace, dot: str, name: str, description: str) -> str:
    """The view link: through a secret gist with `--gist`, else the DOT in the link itself."""
    if not args.gist:
        return bundle.view_url(dot)
    raw = await gist({name: dot}, description, os.environ[TOKEN_ENV])
    return url_view(raw[name])


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
        after_store=args.after_store,
        reference=args.reference,
    )
    made = bundle.from_diff(
        result,
        before=args.before,
        after=args.after,
        system=args.system,
        timed=args.build_times,
        reference=args.reference,
    )
    if args.out is None:
        return made.model_dump_json(indent=2)
    url = await _view(args, bundle.to_dot(made), bundle.DOT_FILE, f"pynix-review diff: {args.after}")
    written = await bundle.write(made, anyio.Path(args.out), url)
    return json.dumps({"files": [str(p) for p in written], "view": url}, indent=2)


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if args.command != "plan" and args.gist and (args.out is None or not os.environ.get(TOKEN_ENV)):
        parser.error(f"--gist needs --out, and a token with the gist scope in {TOKEN_ENV}")
    if args.command == "plan":
        sys.stdout.write(anyio.run(_plan, args) + "\n")
        return
    if args.command == "build":
        # Each build holds a pool thread for its whole run; two more for parse_derived_path.
        huggorm.set_pool_size(args.jobs + 2)
        sys.stdout.write(anyio.run(_build, args) + "\n")
        return
    huggorm.set_pool_size(args.pool_size)

    attrs: list[str] = list(args.attr)
    if args.attrs:
        attrs += [ln.strip() for ln in args.attrs.read_text().splitlines() if ln.strip()]
    if not attrs:
        parser.error("give --attr or --attrs")

    sys.stdout.write(anyio.run(_run, args, attrs) + "\n")
