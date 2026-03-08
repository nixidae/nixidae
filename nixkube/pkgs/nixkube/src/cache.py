# SPDX-License-Identifier: MIT

import asyncio
import logging
from asyncio import Semaphore, sleep
from collections import defaultdict
from pathlib import Path

from .constants import CACHE_ENABLED
from .errors import SubprocessError
from .subprocessing import run_captured, try_console

logger = logging.getLogger("nixkube.cache")

# Prevent concurrent cache uploads of the same store paths.
# Uses frozenset of paths as key (all paths in a copy operation are serialized together)
# and Semaphore as value (asyncio.Semaphore allows concurrent access limits).
# Ensures only one copy_to_cache() call per unique path set can run at a time.
copy_lock: defaultdict[frozenset[Path], Semaphore] = defaultdict(Semaphore)


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


async def copy_to_cache(package_paths: set[Path]) -> None:
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
        logger.debug("copy_to_cache: no package paths to copy")
        return

    logger.debug(f"copy_to_cache: starting for {len(package_paths)} packages")

    async with copy_lock[frozenset(package_paths)]:
        paths: set[Path] = {Path(p) for p in package_paths}

        # Run path-info calls concurrently (regular + derivation)
        path_info, path_info_drv = await asyncio.gather(
            run_captured(
                "nix",
                "path-info",
                "--recursive",
                *package_paths,
            ),
            run_captured(
                "nix",
                "path-info",
                "--recursive",
                "--derivation",
                *package_paths,
            ),
        )

        # Get regular closure paths for all packages
        if path_info.returncode == 0:
            paths.update(Path(p) for p in path_info.stdout.splitlines())
        else:
            logger.warning(
                "Failed to get regular paths",
                extra={"returncode": path_info.returncode, "stderr": path_info.stderr},
            )

        # Try to get derivation paths recursively. This may fail if we only have
        # store paths without .drv files (e.g., fetched from substituters), which is normal.
        if path_info_drv.returncode == 0:
            paths.update(Path(p) for p in path_info_drv.stdout.splitlines())
        else:
            logger.debug(
                "No derivation paths found (normal if fetched from substituters)",
                extra={
                    "returncode": path_info_drv.returncode,
                    "stderr": path_info_drv.stderr,
                },
            )

        # Filter out .drv files and deduplicate
        paths = {p for p in paths if p.suffix != ".drv"}

        if len(paths) > 0:
            sign_result = await run_captured(
                "nix",
                "store",
                "sign",
                "--key-file",
                "/etc/nix-key/nix_ed25519",
                *paths,
            )
            if sign_result.returncode != 0:
                logger.warning(
                    "Failed to sign paths",
                    extra={
                        "returncode": sign_result.returncode,
                        "stderr": sign_result.stderr,
                    },
                )

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
                    logger.debug(f"Successfully copied {len(paths)} paths to cache")
                    break
                else:
                    logger.warning(
                        f"Copy attempt {attempt + 1}/6 failed",
                        extra={
                            "returncode": nix_copy.returncode,
                            "stdout": nix_copy.stdout,
                            "stderr": nix_copy.stderr,
                        },
                    )
            else:
                logger.error(
                    f"Failed to copy to cache after 6 attempts: {len(paths)} paths"
                )


def schedule_copy_to_cache(package_paths: set[Path]) -> None:
    """Fire-and-forget background task to copy packages to cache."""
    if not package_paths:
        return
    task = asyncio.create_task(copy_to_cache(package_paths))
    task.add_done_callback(
        lambda t: (
            logger.error(f"copy_to_cache failed: {t.exception()}")
            if t.exception()
            else None
        )
    )
