from __future__ import annotations

import argparse
import sys
from pathlib import Path

import huggorm

from .app import ModiffApp
from .engine import EngineThread, NixLog, Source, nixos_source
from .model import Edit, parse_option_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="modiff",
        description="Edit a Nix module system configuration and see what the edit changes.",
    )
    which = parser.add_mutually_exclusive_group()
    which.add_argument(
        "--nixos", type=Path, metavar="FILE", help="a NixOS configuration module (default: a minimal one)"
    )
    which.add_argument("--expr", help="an expression that evaluates to an evalModules result")
    parser.add_argument(
        "--nixpkgs",
        default="<nixpkgs>",
        help="the nixpkgs a NixOS configuration evaluates against (default: <nixpkgs>)",
    )
    parser.add_argument(
        "--force",
        action="append",
        default=[],
        metavar="PATH",
        help="a path under the --expr result to evaluate first, such as config.system.build.toplevel.drvPath",
    )
    parser.add_argument("--set", action="append", default=[], metavar="OPTION=EXPR", help="an edit to start with")
    args = parser.parse_args(argv)
    # A directory is fine: the module system imports its `default.nix`.
    if args.nixos is not None and not args.nixos.exists():
        parser.error(f"--nixos {args.nixos}: no such file or directory")
    return args


def source_of(args: argparse.Namespace) -> Source:
    if args.expr is not None:
        return Source(args.expr, tuple(parse_option_path(p) for p in args.force), base_dir=str(Path.cwd()))
    nixos = None if args.nixos is None else str(args.nixos.resolve())
    return nixos_source(nixos, args.nixpkgs)


def edits_of(args: argparse.Namespace) -> list[Edit]:
    edits: list[Edit] = []
    for item in args.set:
        path, sep, expr = item.partition("=")
        if not sep:
            raise SystemExit(f"modiff: --set {item!r} has no '='")
        edits.append(Edit(parse_option_path(path.strip()), expr.strip()))
    return edits


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    # What `nix` does at startup: without it, `experimental-features` is
    # empty and a configuration that calls `builtins.fetchTree` fails.
    huggorm.load_config()
    huggorm.init_plugins()
    nix_log = NixLog.subscribe()
    try:
        ModiffApp(EngineThread(source_of(args)), edits_of(args), nix_log).run()
    finally:
        # Once, now that the terminal is back: the TUI kept them off it.
        nix_log.collect()
        for text, times in nix_log.messages.items():
            print(text if times == 1 else f"{text} ({times} times)", file=sys.stderr)
