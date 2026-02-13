import logging
import shutil
import time
from pathlib import Path

from .errors import MountError, UnmountError

from .constants import (
    CSI_GCROOTS,
    CSI_VOLUMES,
    MOUNT_ALREADY_MOUNTED,
    NIX_BUILD_TIMEOUT,
    VERIFY_STORE_PATHS,
)
from .hardlinks import deref_hardlink_tree, hardlink_closure
from .nix import get_closure_paths, init_database, install_gcroots, install_result_link, verify_store_paths
from .subprocessing import run_captured, run_console

logger = logging.getLogger("nix-csi")

async def prepare_volume(
    volume_id: str,
    package_paths: list[Path],
    primary_package: Path | None,
) -> Path:
    """
    Prepare a volume root with hardlinked store paths and initialized database.

    Returns the volume_root path.
    """
    gc_root = CSI_GCROOTS / volume_id
    volume_root = CSI_VOLUMES / volume_id

    # Capitalized to emphasise they're Nix environment variables
    NIX_STATE_DIR = volume_root / "nix/var/nix"
    NIX_STATE_DIR.mkdir(parents=True, exist_ok=True)

    # Verify all packages and their closures before processing
    if VERIFY_STORE_PATHS:
        await verify_store_paths(package_paths)

    # Get storepaths from all packages
    store_paths = await get_closure_paths(package_paths)

    # This block is essentially nix copy into a chroot store with
    # extra steps. (Hardlinking instead of dumbcopying)

    # Copy closure to substore
    hardlink_start = time.perf_counter()
    hardlink_closure([Path(p) for p in store_paths], volume_root / "nix/store")
    logger.debug(f"Hardlinked {len(store_paths)} paths in {time.perf_counter() - hardlink_start:.2f}s")

    # Create Nix database
    await init_database(NIX_STATE_DIR, store_paths)

    # Install gcroots in container using chroot store. This is
    # required because the auto roots created for /nix/var/result
    # will point to Narnia while this one points into store.
    await install_gcroots(
        package_paths,
        NIX_STATE_DIR / "gcroots" / "csi",
        store=volume_root,
        timeout=NIX_BUILD_TIMEOUT,
    )

    # Install /nix/var/result in container using chroot store
    if primary_package is not None:
        await install_result_link(volume_root, primary_package)
        # Create hardlink farm of primary package to volume_root
        deref_start = time.perf_counter()
        deref_hardlink_tree(primary_package, volume_root)
        logger.debug(f"Dereferenced hardlink tree in {time.perf_counter() - deref_start:.2f}s")

    return volume_root


async def mount_volume(
    volume_root: Path,
    target_path: Path,
    readonly: bool,
) -> None:
    """Mount the volume root to the target path."""
    target_path.mkdir(parents=True, exist_ok=True)

    if readonly:
        # For readonly we use a bind mount, the benefit is that different
        # container stores using bindmounts will get the same inodes and
        # share page cache with others, reducing memory usage.
        mount_command = [
            "mount",
            "--verbose",
            "--bind",
            "-o",
            "ro",
            volume_root,
            target_path,
        ]
    else:
        # For readwrite we use an overlayfs mount, the benefit here is that
        # it works as CoW even if the underlying filesystem doesn't support
        # it, reducing host storage usage.
        workdir = volume_root / "workdir"
        upperdir = volume_root / "upperdir"
        workdir.mkdir(parents=True, exist_ok=True)
        upperdir.mkdir(parents=True, exist_ok=True)
        mount_command = [
            "mount",
            "--verbose",
            "-t",
            "overlay",
            "overlay",
            "-o",
            f"rw,lowerdir={volume_root},upperdir={upperdir},workdir={workdir}",
            target_path,
        ]

    mount = await run_console(*mount_command)
    if mount.returncode == MOUNT_ALREADY_MOUNTED:
        pass  # Already mounted is fine
    elif mount.returncode != 0:
        raise MountError(
            f"Failed to mount volume (exit code {mount.returncode})",
            logs=mount.combined,
        )


def cleanup_failed_volume(gc_root: Path, volume_root: Path) -> None:
    """Clean up resources after a failed volume operation."""
    shutil.rmtree(gc_root, ignore_errors=True)
    shutil.rmtree(volume_root, ignore_errors=True)


async def is_mount(path: Path) -> bool:
    """Check if a path is a mount point."""
    return (await run_captured("findmnt", "--mountpoint", path)).returncode == 0


async def unmount(path: Path) -> None:
    """Unmount a path. Raises UnmountError on failure if still mounted."""
    result = await run_captured("umount", "--verbose", path)
    if result.returncode != 0 and await is_mount(path):
        raise UnmountError(
            f"Failed to unmount volume (exit code {result.returncode})",
            logs=result.combined,
        )
