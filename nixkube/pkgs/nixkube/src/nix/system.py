# SPDX-License-Identifier: MIT

import subprocess
from functools import cache

from ..errors import SystemDetectionError


@cache
def get_current_system() -> str:
    """Get system string evaluated by nix (cached after first call)."""
    try:
        result = subprocess.run(
            [
                "nix",
                "eval",
                "--raw",
                "--impure",
                "--store",
                "dummy://",
                "--expr",
                "builtins.currentSystem",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise SystemDetectionError(
            "Failed to detect system type",
            logs=str(e),
        ) from e
