"""A trust bundle for host programs: the system's roots and the lab's.

`SSL_CERT_FILE` replaces a program's trust store rather than adding to it,
so the file it names must hold the public roots too.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

VARIABLES: Final = ("SSL_CERT_FILE", "NIX_SSL_CERT_FILE")
"""Where the caller already points its programs, first match wins."""


def system_roots(environ: Mapping[str, str], candidates: list[Path]) -> Path | None:
    """The first existing file of the caller's variables, then of *candidates*."""
    named = [Path(environ[variable]) for variable in VARIABLES if environ.get(variable)]
    return next((path for path in [*named, *candidates] if path.is_file()), None)


def bundle(system: str, root: str) -> str:
    """*system* then *root*, each ending in a newline. *system* as it is
    when it holds *root*: a shell that exported an earlier run's bundle
    names it as its system roots."""
    if root.strip() in system:
        return system
    parts = [part if part.endswith("\n") else part + "\n" for part in (system, root) if part]
    return "".join(parts)
