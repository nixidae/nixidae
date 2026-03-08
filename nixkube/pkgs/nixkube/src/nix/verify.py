# SPDX-License-Identifier: MIT

from pathlib import Path

import structlog

from ..errors import SubprocessError, VerifyStorePathsError
from ..subprocessing import try_captured

logger = structlog.get_logger("nixkube.nix")


async def verify_store_paths(package_paths: set[Path]) -> None:
    """Verify the integrity of all packages and their closures."""
    try:
        await try_captured(
            "nix",
            "store",
            "verify",
            "--recursive",
            *package_paths,
        )
    except SubprocessError as e:
        raise VerifyStorePathsError(
            "Failed to verify store paths",
            logs=e.combined,
        ) from e
