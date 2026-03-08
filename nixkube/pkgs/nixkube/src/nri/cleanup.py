# SPDX-License-Identifier: MIT
"""NRI container cleanup and garbage collection."""

import shutil
from pathlib import Path

import structlog

from ..constants import HOST_ROOT, NRI_CONTAINERS
from ..cri import list_container_ids

logger = structlog.get_logger("nixkube.nri.cleanup")


async def garbage_collect_stale_volumes(cri_socket: Path) -> None:
    """Remove volumes for containers no longer in CRI.

    Queries the CRI to get the list of active containers and removes any
    stale volume directories for containers that are no longer running.
    """
    try:
        # Get list of active containers from CRI
        # Access socket through host mount since we're in a container
        socket_path = HOST_ROOT / cri_socket.relative_to("/")
        active_ids = await list_container_ids(socket_path)
        logger.debug("gc_active_containers", count=len(active_ids))

        # Clean up volumes for containers not in active list
        if NRI_CONTAINERS.exists():
            stale_count = 0
            for volume_dir in NRI_CONTAINERS.iterdir():
                if volume_dir.is_dir() and volume_dir.name not in active_ids:
                    try:
                        shutil.rmtree(volume_dir)
                        stale_count += 1
                        logger.debug(
                            "gc_removed_stale_volume", container=volume_dir.name
                        )
                    except Exception:
                        logger.warning(
                            "gc_remove_failed",
                            volume=str(volume_dir),
                            exc_info=True,
                        )
            if stale_count > 0:
                logger.info("gc_cleaned_nri_volumes", count=stale_count)
    except Exception:
        logger.warning("gc_failed", exc_info=True)
