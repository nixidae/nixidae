"""`vivarium-socks --listen HOST:PORT [--domain SUFFIX=SERVER[,SERVER]] [--zone FILE]`."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

import anyio

from .resolve import Resolver, Zone
from .server import Resolving, serve


def domain(text: str) -> tuple[str, list[str]]:
    suffix, sep, servers = text.partition("=")
    if not sep or not suffix or not servers:
        raise argparse.ArgumentTypeError(f"{text!r} is not SUFFIX=SERVER[,SERVER]")
    return suffix, servers.split(",")


def listen(text: str) -> tuple[str, int]:
    host, sep, port = text.rpartition(":")
    if not sep or not port.isdigit():
        raise argparse.ArgumentTypeError(f"{text!r} is not HOST:PORT")
    return host.strip("[]"), int(port)


def parse(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="vivarium-socks", description="A SOCKS5 proxy into a vivarium lab.")
    parser.add_argument("--listen", type=listen, required=True, metavar="HOST:PORT")
    parser.add_argument(
        "--domain",
        type=domain,
        action="append",
        default=[],
        metavar="SUFFIX=SERVER[,SERVER]",
        help="resolve names under SUFFIX at these DNS servers (repeatable)",
    )
    parser.add_argument(
        "--zone",
        type=Path,
        metavar="FILE",
        help="a JSON zone of the lab's own names and SRV records, answered with no DNS server",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")
    host, port = args.listen
    zone = None if args.zone is None else Zone.from_json(args.zone.read_text())
    anyio.run(serve, host, port, Resolving(Resolver(dict(args.domain), zone=zone)))


if __name__ == "__main__":
    main()
