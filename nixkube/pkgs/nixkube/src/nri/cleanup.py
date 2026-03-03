# SPDX-License-Identifier: MIT
"""NRI container cleanup and garbage collection."""

import logging
import shutil
from pathlib import Path

from ..constants import NRI_CONTAINERS
from ..cri import list_container_ids

logger = logging.getLogger("nixkube.nri.cleanup")


async def garbage_collect_stale_volumes(cri_socket: Path) -> None:
    """Remove volumes for containers no longer in CRI.

    Queries the CRI to get the list of active containers and removes any
    stale volume directories for containers that are no longer running.
    """
    try:
        # Get list of active containers from CRI
        # Access socket through host mount since we're in a container
        socket_path = Path("/host") / cri_socket.relative_to("/")
        active_ids = await list_container_ids(socket_path)
        logger.debug(
            "GC: Active containers from CRI: %d",
            len(active_ids),
        )

        # Clean up volumes for containers not in active list
        if NRI_CONTAINERS.exists():
            stale_count = 0
            for volume_dir in NRI_CONTAINERS.iterdir():
                if volume_dir.is_dir() and volume_dir.name not in active_ids:
                    try:
                        shutil.rmtree(volume_dir)
                        stale_count += 1
                        logger.debug(
                            "GC: Removed stale volume for container=%r",
                            volume_dir.name,
                        )
                    except Exception as e:
                        logger.warning(
                            "GC: Failed to remove stale volume at %r: %s",
                            volume_dir,
                            e,
                        )
            if stale_count > 0:
                logger.info("GC: Cleaned up %d stale NRI volumes", stale_count)
    except Exception as e:
        logger.warning(
            "GC: Failed to perform garbage collection: %s",
            e,
        )
