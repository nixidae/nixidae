"""The variables a run hands `vivarium env` and `vivarium shell`.

Phases add to them through `vms.export`. They live in the run, and the
CLI asks the live run for them (`Op.ENV`): a stopped run's variables name
ports another run may hold now, so no file keeps them.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PROXY: Final = "VIVARIUM_PROXY"
"""The session's split proxy on the host, as a `socks5h://` URL, when the
router serves a SOCKS5 proxy: lab names and addresses through the
router's, the rest direct."""

ENVIRONMENT: Final = "VIVARIUM_ENVIRONMENT"
OUT: Final = "VIVARIUM_OUT"

PROXY_VARIABLES: Final = ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
"""curl reads `http_proxy` in lower case only; Go and Python read both."""
NO_PROXY_VARIABLES: Final = ("NO_PROXY", "no_proxy")
NO_PROXY: Final = "localhost,127.0.0.0/8,::1"
"""The host's own forwards, a kubeconfig's API server among them. Go leaves
loopback alone by itself; curl does not."""


class ExportError(ValueError):
    pass


class Exports:
    """`vivarium_runner.Exports` for one run: one object every phase shares."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    async def set(self, name: str, value: str) -> None:
        if not NAME.fullmatch(name):
            raise ExportError(f"{name!r} is no environment variable name")
        self._values[name] = value

    @property
    def values(self) -> dict[str, str]:
        return dict(self._values)


def shell(exported: Mapping[str, str], *, environment: str | None, out: Path, proxied: bool = True) -> dict[str, str]:
    """What `vivarium env` prints and `vivarium shell` sets, less `unproxy` on PATH. A phase that
    runs a host program with these runs it as a developer's `vivarium shell` does."""
    found = dict(exported)
    found[OUT] = str(out)
    if environment is not None:
        found[ENVIRONMENT] = environment
    proxy = exported.get(PROXY)
    if proxied and proxy is not None:
        found |= dict.fromkeys(PROXY_VARIABLES, proxy) | dict.fromkeys(NO_PROXY_VARIABLES, NO_PROXY)
    return found


def encode(values: dict[str, str]) -> str:
    return json.dumps(values, sort_keys=True)


def decode(text: str) -> dict[str, str]:
    """An `Op.ENV` reply's variables. Raises `ExportError`."""
    try:
        found = json.loads(text)
    except ValueError as error:
        raise ExportError(f"the run's variables are no JSON: {error}") from None
    if not isinstance(found, dict) or not all(
        isinstance(key, str) and NAME.fullmatch(key) and isinstance(value, str) for key, value in found.items()
    ):
        raise ExportError("the run's variables are not an object of variable names to strings")
    return found
