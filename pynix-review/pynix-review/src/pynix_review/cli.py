"""`pynix-review diff`: the review bundle of two nixpkgs trees.

With `--out DIR` it writes `bundle.json`, `bundle.schema.json`,
`changed.dot` and `changed.url` there, and prints the paths and the view
link as JSON. Without it, it prints `bundle.json`.

`pynix-review plan BUNDLE`: what would build within a build-time budget, by the estimates.
`pynix-review build BUNDLE`: build what fits in a wall-clock budget, a result per derivation.
`pynix-review pr N --out DIR`: a nixpkgs pull request, from GitHub to `built.md`: diff and build.
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
from .build import (
    DAEMON,
    DOT_FILE as BUILT_DOT,
    JOBS,
    KILL_AFTER_SECONDS,
    WALL_SECONDS,
    build,
    write as write_built,
)
from .buildtimes import UNREPORTED_PHASE_SECONDS
from .evaluate import SCRATCH_STORE
from .github import NIXPKGS, PullRequest, pull_request, tarball
from .plan import BUDGET_SECONDS, UNTIMED_SECONDS, plan
from .review import diff
from .share import TOKEN_ENV, gist, url_view

CHANNEL = "https://channels.nixos.org/nixpkgs-unstable/nixexprs.tar.xz"
GIST_HELP = f"with --out, put the DOT in a secret gist and link to it ({TOKEN_ENV} needs the gist scope)"
POOL_SIZE = 4
MAX_DERIVATIONS = 1000
PR_FILE = "pr.json"
BUNDLE_DIR = "bundle"
BUILT_DIR = "built"


def _diff_options(cmd: argparse.ArgumentParser) -> None:
    cmd.add_argument("--system", default=huggorm.current_system())
    cmd.add_argument("--hydra", action="store_true", help="with --build-times, ask hydra.nixos.org for exact times")
    cmd.add_argument(
        "--phase-seconds",
        type=float,
        default=UNREPORTED_PHASE_SECONDS,
        help="seconds assumed for a logged phase that printed no footer (under 30 s)",
    )
    cmd.add_argument("--pool-size", type=int, default=POOL_SIZE, help="huggorm's thread pool, for the closure walk")


def _build_options(cmd: argparse.ArgumentParser) -> None:
    cmd.add_argument("--wall", type=int, default=WALL_SECONDS, help="seconds to start builds in")
    cmd.add_argument(
        "--kill-after",
        type=int,
        default=KILL_AFTER_SECONDS,
        help="seconds after which running builds are stopped (at least --wall)",
    )
    cmd.add_argument("--jobs", type=int, default=JOBS, help="builds at once")
    cmd.add_argument(
        "--build-timeout",
        type=int,
        default=0,
        help="seconds one build may run before Nix stops it (0: the daemon's own)",
    )
    cmd.add_argument(
        "--max-silent-time",
        type=int,
        default=0,
        help="seconds a build may print nothing before Nix stops it (0: the daemon's own)",
    )
    cmd.add_argument(
        "--untimed-seconds",
        type=int,
        default=UNTIMED_SECONDS,
        help="seconds assumed for a derivation with no time, for the order",
    )
    cmd.add_argument("--print-build-logs", action="store_true", help="each builder's output and Nix's info on stderr")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pynix-review", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    cmd = commands.add_parser("diff", help="what a nixpkgs change rebuilds, layered by depth")
    cmd.add_argument("--before", required=True, help="nixpkgs directory or tarball URL")
    cmd.add_argument("--after", required=True, help="nixpkgs directory or tarball URL")
    cmd.add_argument("--attrs", type=Path, help="file with one attribute path per line")
    cmd.add_argument("--attr", action="append", default=[])
    cmd.add_argument("--after-overlay", help="a Nix overlay applied to the after tree only")
    cmd.add_argument("--build-times", action="store_true", help="time changed derivations from cache.nixos.org logs")
    cmd.add_argument(
        "--after-store",
        default=SCRATCH_STORE,
        help="store for the after side's .drv files; `daemon` makes them buildable",
    )
    cmd.add_argument(
        "--reference",
        help=f"with --build-times, a tree Hydra has built, for times the merge base lacks; e.g. {CHANNEL}",
    )
    _diff_options(cmd)
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
    _build_options(cmd)
    cmd.add_argument("--store", default=DAEMON, help="the store that builds; the daemon picks the machines")
    cmd.add_argument("--out", type=Path, help="write built.json, its schema, built.dot, built.url and built.md here")
    cmd.add_argument("--gist", action="store_true", help=GIST_HELP)

    cmd = commands.add_parser(
        "pr",
        help=f"a pull request from GitHub: CI's rebuild list, diff against the merge base, build ({TOKEN_ENV})",
    )
    cmd.add_argument("number", type=int)
    cmd.add_argument("--repo", default=NIXPKGS)
    cmd.add_argument("--out", type=Path, required=True, help=f"{PR_FILE}, {BUNDLE_DIR}/ and {BUILT_DIR}/ go here")
    cmd.add_argument("--reference", default=CHANNEL, help="a tree Hydra has built, for times the merge base lacks")
    cmd.add_argument(
        "--max-derivations",
        type=int,
        default=MAX_DERIVATIONS,
        help="build nothing when the change rebuilds more derivations than this",
    )
    _diff_options(cmd)
    _build_options(cmd)
    cmd.add_argument("--gist", action="store_true", help=GIST_HELP)
    return parser


async def _view(args: argparse.Namespace, dot: str, name: str, description: str) -> str:
    """The view link: through a secret gist with `--gist`, else the DOT in the link itself."""
    if not args.gist:
        return bundle.view_url(dot)
    raw = await gist({name: dot}, description, os.environ[TOKEN_ENV])
    return url_view(raw[name])


def _files(written: list[anyio.Path], view: str) -> dict[str, object]:
    return {"files": [str(p) for p in written], "view": view}


async def _built(args: argparse.Namespace, made: bundle.Bundle, store: str, out: Path | None) -> dict[str, object]:
    """Build `made`; with `out`, its files and their view link, else the report."""
    result = await build(
        made,
        wall=args.wall,
        kill_after=args.kill_after,
        jobs=args.jobs,
        untimed_seconds=args.untimed_seconds,
        store_url=store,
        build_logs=args.print_build_logs,
        build_timeout=args.build_timeout,
        max_silent_time=args.max_silent_time,
    )
    if out is None:
        return result.model_dump(mode="json")
    url = await _view(args, bundle.to_dot(made, result), BUILT_DOT, f"pynix-review build: {made.after}")
    return _files(await write_built(result, made, anyio.Path(out), url), url)


async def _build(args: argparse.Namespace) -> str:
    made = bundle.Bundle.model_validate_json(await anyio.Path(args.bundle).read_text())
    return json.dumps(await _built(args, made, args.store, args.out), indent=2)


async def _plan(args: argparse.Namespace) -> str:
    made = bundle.Bundle.model_validate_json(await anyio.Path(args.bundle).read_text())
    return plan(made, budget=args.budget, untimed_seconds=args.untimed_seconds).model_dump_json(indent=2)


async def _bundle(  # noqa: PLR0913 -- keyword-only, one per diff input
    args: argparse.Namespace,
    attrs: list[str],
    *,
    before: str,
    after: str,
    build_times: bool,
    after_store: str,
    reference: str | None,
    after_overlay: str | None = None,
) -> bundle.Bundle:
    result = await diff(
        before,
        after,
        attrs,
        system=args.system,
        after_overlay=after_overlay,
        build_times=build_times,
        hydra=args.hydra,
        per_phase=args.phase_seconds,
        after_store=after_store,
        reference=reference,
    )
    return bundle.from_diff(
        result, before=before, after=after, system=args.system, timed=build_times, reference=reference
    )


async def _run(args: argparse.Namespace, attrs: list[str]) -> str:
    made = await _bundle(
        args,
        attrs,
        before=args.before,
        after=args.after,
        build_times=args.build_times,
        after_store=args.after_store,
        reference=args.reference,
        after_overlay=args.after_overlay,
    )
    if args.out is None:
        return made.model_dump_json(indent=2)
    url = await _view(args, bundle.to_dot(made), bundle.DOT_FILE, f"pynix-review diff: {args.after}")
    return json.dumps(_files(await bundle.write(made, anyio.Path(args.out), url), url), indent=2)


def skipped(pr: PullRequest, made: bundle.Bundle | None, max_derivations: int) -> str | None:
    """Why `pr` builds nothing, or None."""
    if pr.attrs is None:
        return "CI's comparison artifact is missing"
    if not pr.attrs:
        return "CI lists no rebuild"
    if made is not None and len(made.derivations) > max_derivations:
        return f"{len(made.derivations)} changed derivations, more than {max_derivations}"
    return None


async def _pr(args: argparse.Namespace) -> str:
    out = anyio.Path(args.out)
    await out.mkdir(parents=True, exist_ok=True)
    pr = await pull_request(args.number, token=os.environ[TOKEN_ENV], system=args.system, repo=args.repo)
    await (out / PR_FILE).write_text(pr.model_dump_json(indent=2) + "\n")
    answer: dict[str, object] = {"pr": pr.model_dump(mode="json"), "skipped": skipped(pr, None, args.max_derivations)}
    if answer["skipped"] is not None or pr.attrs is None:
        return json.dumps(answer, indent=2)
    made = await _bundle(
        args,
        pr.attrs,
        before=tarball(pr.repo, pr.merge_base),
        after=tarball(pr.repo, pr.head),
        build_times=True,
        after_store=DAEMON,
        reference=args.reference or None,
    )
    view = await _view(args, bundle.to_dot(made), bundle.DOT_FILE, f"pynix-review diff: {pr.repo}#{pr.number}")
    answer["bundle"] = _files(await bundle.write(made, out / BUNDLE_DIR, view), view)
    answer["skipped"] = skipped(pr, made, args.max_derivations)
    if answer["skipped"] is None:
        answer["built"] = await _built(args, made, DAEMON, Path(args.out) / BUILT_DIR)
    return json.dumps(answer, indent=2)


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if args.command != "plan" and args.gist and (args.out is None or not os.environ.get(TOKEN_ENV)):
        parser.error(f"--gist needs --out, and a token with the gist scope in {TOKEN_ENV}")
    if args.command in {"build", "pr"} and args.kill_after < args.wall:
        parser.error("--kill-after must be at least --wall")
    if args.command == "pr" and not os.environ.get(TOKEN_ENV):
        parser.error(f"pr needs a GitHub token in {TOKEN_ENV}: GitHub serves CI's artifacts only with one")
    if args.command == "plan":
        sys.stdout.write(anyio.run(_plan, args) + "\n")
        return
    if args.command in {"build", "pr"}:
        # Each build holds a pool thread for its whole run; two more for parse_derived_path.
        walk = args.pool_size if args.command == "pr" else POOL_SIZE
        huggorm.set_pool_size(max(args.jobs + 2, walk))
        sys.stdout.write(anyio.run(_build if args.command == "build" else _pr, args) + "\n")
        return
    huggorm.set_pool_size(args.pool_size)

    attrs: list[str] = list(args.attr)
    if args.attrs:
        attrs += [ln.strip() for ln in args.attrs.read_text().splitlines() if ln.strip()]
    if not attrs:
        parser.error("give --attr or --attrs")

    sys.stdout.write(anyio.run(_run, args, attrs) + "\n")
