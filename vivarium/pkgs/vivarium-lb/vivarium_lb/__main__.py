"""`vivarium-lb CONFIG`: the router's LoadBalancer controller."""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from pathlib import Path

import anyio

from .controller import Config, run


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="vivarium-lb", description="LoadBalancer Services for a vivarium lab.")
    parser.add_argument("config", type=Path, help="the JSON config the router module writes")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # httpx logs every request at INFO; the journal wants the decisions.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    anyio.run(run, Config.from_json(json.loads(args.config.read_text())))


if __name__ == "__main__":
    main()
