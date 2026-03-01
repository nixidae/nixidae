# SPDX-License-Identifier: MIT

import json
import logging
import shutil
from pathlib import Path

from .constants import CSI_GCROOTS, CSI_VOLUMES, KUBELET_PODS_PATH

logger = logging.getLogger("nix-csi")


def collect_active_volume_handles(exclude_vol_data_path: Path | None) -> set[str]:
    """
    Collect volumeHandle values from all active CSI volumes on this node.

    Scans kubelet's pod volume directories for vol_data.json files and extracts
    the volumeHandle from each. This provides a snapshot of which volumes are
    currently in use by pods.
    """
    active_handles: set[str] = set()

    for vol_data_path in KUBELET_PODS_PATH.glob(
        "*/volumes/kubernetes.io~csi/*/vol_data.json"
    ):
        if exclude_vol_data_path and vol_data_path == exclude_vol_data_path:
            continue

        try:
            data = json.loads(vol_data_path.read_text())
            if handle := data.get("volumeHandle"):
                active_handles.add(handle)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Failed to read {vol_data_path}: {e}")

    return active_handles


def cleanup_stale_entries(active_handles: set[str]) -> None:
    """
    Remove gcroot and volume directories that are no longer in use.

    Iterates through CSI_GCROOTS and CSI_VOLUMES directories and removes any
    entries whose name (volume ID) is not in the set of active volume handles.
    """
    for gcroot in CSI_GCROOTS.iterdir():
        if gcroot.name not in active_handles:
            try:
                shutil.rmtree(gcroot, ignore_errors=True)
                logger.debug(f"Removed stale gcroot {gcroot}")
            except Exception as e:
                logger.warning(f"Failed to remove stale gcroot {gcroot}: {e}")

    for volume in CSI_VOLUMES.iterdir():
        if volume.name not in active_handles:
            try:
                shutil.rmtree(volume)
                logger.debug(f"Removed stale volume {volume}")
            except Exception as e:
                logger.warning(f"Failed to remove stale volume {volume}: {e}")
