# SPDX-License-Identifier: MIT

import asyncio
import logging
from asyncio import Semaphore, sleep
from collections import defaultdict
from pathlib import Path

from .constants import CACHE_ENABLED
from .errors import SubprocessError
from .subprocessing import run_captured, try_console

logger = logging.getLogger("nix-csi")

# Locks that prevent the same derivation to be uploaded in parallel
copy_lock: defaultdict[Path, Semaphore] = defaultdict(Semaphore)


async def check_cache_connectivity() -> bool:
    """Check if the cache is reachable via SSH."""
    if not CACHE_ENABLED:
        return False

    try:
        logger.debug("Trying to connect to cache")
        await asyncio.wait_for(
            try_console(
                "nix",
                "store",
                "ping",
                "--store",
                "ssh-ng://nix@nix-cache",
                log_level=logging.DEBUG,
            ),
            timeout=10.0,
        )
        logger.debug("Cache connectivity check succeeded")
        return True
    except (SubprocessError, OSError, asyncio.TimeoutError):
        logger.warning("Cache connectivity check failed")
        return False


def get_substituter_args() -> list[str]:
    """Get nix command arguments for using the cache as a substituter."""
    return [
        "--extra-substituters",
        "ssh-ng://nix@nix-cache?trusted=1&priority=20",
    ]


async def copy_to_cache(package_paths: list[Path]) -> None:
    """
    Copy packages and their closures to the cache.

    TODO: Rewrite this entire copy process to support user-supplied copy scripts.
    This will allow end-users to copy to arbitrary destinations (S3, GCS, custom caches, etc.)
    rather than hard-coding ssh-ng://nix@nix-cache.

    TODO: Building should be moved to separate builder pods rather than happening
    within the CSI daemonset. The daemonset should only handle mounting pre-built paths.
    This will improve separation of concerns and allow dedicated builder infrastructure.
    """
    if not package_paths:
        return

    # Create a lock key from all paths to prevent concurrent copies of the same set
    lock_key = tuple(sorted(package_paths))
    async with copy_lock[lock_key[0] if lock_key else Path("")]:
        paths: set[Path] = {Path(p) for p in package_paths}

        # Get regular closure paths for all packages
        path_info = await run_captured(
            "nix",
            "path-info",
            "--recursive",
            *package_paths,
        )
        if path_info.returncode == 0:
            paths.update(Path(p) for p in path_info.stdout.splitlines())
        else:
            logger.debug("Failed to get regular paths for packages")

        # Try to get derivation paths recursively. This may fail if we only have
        # store paths without .drv files (e.g., fetched from substituters), which is normal.
        path_info_drv = await run_captured(
            "nix",
            "path-info",
            "--recursive",
            "--derivation",
            *package_paths,
        )
        if path_info_drv.returncode == 0:
            paths.update(Path(p) for p in path_info_drv.stdout.splitlines())
        else:
            logger.debug(
                "No derivation paths found for packages (normal if fetched from substituters)"
            )

        # Filter out .drv files and deduplicate
        paths = {p for p in paths if p.suffix != ".drv"}

        if len(paths) > 0:
            for attempt in range(6):
                if attempt > 0:
                    exp_backoff = min(5 * (2 ** (attempt - 1)), 60)
                    logger.warning(
                        f"Retry {attempt}/6 copying to cache after {exp_backoff}s: {len(paths)} paths"
                    )
                    await sleep(exp_backoff)

                nix_copy = await run_captured(
                    "nix", "copy", "--to", "ssh-ng://nix@nix-cache", *paths
                )
                if nix_copy.returncode == 0:
                    logger.debug(f"Successfully copied to cache: {len(paths)} paths")
                    break
                else:
                    logger.debug(
                        f"Copy attempt {attempt + 1}/6 failed: {nix_copy.combined}"
                    )
            else:
                logger.error(
                    f"Failed to copy to cache after 6 attempts: {len(paths)} paths"
                )
