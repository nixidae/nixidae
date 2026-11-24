"""`access/environment.json`: the variables a run hands `vivarium shell`.

Phases add to it through `vms.export`; the CLI only reads it. A new run
empties `access/`, so a reused `--out` never hands over the last run's.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Final

import anyio

if TYPE_CHECKING:
    from pathlib import Path

FILE: Final = "environment.json"
NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PROXY: Final = "VIVARIUM_PROXY"
"""The session's split proxy on the host, as a `socks5h://` URL, when the
router serves a SOCKS5 proxy: lab names and addresses through the
router's, the rest direct."""


class ExportError(ValueError):
    pass


class Exports:
    """`vivarium_runner.Exports` for one run: one object every phase shares,
    so two phases that run at once both land in the file."""

    def __init__(self, access: Path) -> None:
        self._file = access / FILE
        self._values: dict[str, str] = {}
        self._lock = anyio.Lock()

    async def set(self, name: str, value: str) -> None:
        if not NAME.fullmatch(name):
            raise ExportError(f"{name!r} is no environment variable name")
        async with self._lock:
            self._values[name] = value
            # A rename: `vivarium env` reads the file while the run goes.
            partial = anyio.Path(self._file.with_name(f".{FILE}.partial"))
            await partial.write_text(json.dumps(self._values, indent=2, sort_keys=True) + "\n")
            await partial.rename(self._file)


def read(access: Path) -> dict[str, str]:
    """The run's variables; none when no phase exported any."""
    try:
        found = json.loads((access / FILE).read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(found, dict) or not all(
        isinstance(key, str) and NAME.fullmatch(key) and isinstance(value, str) for key, value in found.items()
    ):
        raise ExportError(f"{access / FILE} is not an object of variable names to strings")
    return found
