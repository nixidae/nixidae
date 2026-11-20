"""The length limit on a unix socket's path."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Final

UNIX_PATH_MAX: Final = 108 if sys.platform == "linux" else 104
"""``sizeof(struct sockaddr_un.sun_path)``, including the NUL; 104 on macOS.
A longer path fails at ``bind`` or ``connect``, and a program that binds it
for a guest (UML's mconsole, gvproxy) often says so in one line of a long log."""


def fits(path: Path | str) -> bool:
    """Whether *path* fits in ``sun_path`` with its NUL."""
    return len(os.fsencode(path)) < UNIX_PATH_MAX


__all__ = ["UNIX_PATH_MAX", "fits"]
