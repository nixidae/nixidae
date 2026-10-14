"""Reading what the Nix half set.

This module holds no import of nanopynix, so `_select` stays a module about
packaging and nothing else.
"""

from __future__ import annotations

import os


def env(name: str) -> str:
    """Read one environment variable that the Nix half must set.

    **A large value arrives as a file.** Linux refuses an environment string
    over 128 KiB, so `mkPlanner` passes `PYDD_INPUTS` with `passAsFile`, and
    Nix sets `<name>Path` to the file instead.
    """
    if name not in os.environ and (path := os.environ.get(f"{name}Path")):
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is empty: the Nix half must set it")
    return value
