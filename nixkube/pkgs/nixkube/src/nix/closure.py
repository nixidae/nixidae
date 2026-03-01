# SPDX-License-Identifier: MIT

import logging
from pathlib import Path

from ..errors import StorePathClosureError, SubprocessError
from ..subprocessing import try_captured

logger = logging.getLogger("nix-csi")


async def get_closure_paths(package_paths: set[Path]) -> set[Path]:
    """Get all store paths in the closure of the given packages."""
    try:
        return {
            Path(p)
            for p in (
                await try_captured(
                    "nix",
                    "path-info",
                    "--recursive",
                    *package_paths,
                )
            ).stdout.splitlines()
        }
    except SubprocessError as e:
        raise StorePathClosureError(
            "Failed to get store path closure",
            logs=e.combined,
        ) from e
