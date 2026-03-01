# SPDX-License-Identifier: MIT

import logging
import os
from pathlib import Path

from .errors import HardlinkClosureError

logger = logging.getLogger("nix-csi")


def hardlink_tree(src: Path, dst: Path) -> None:
    """Hardlink a single path (file or directory tree), preserving symlinks."""
    if src.is_symlink():
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(os.readlink(src), dst)
    elif src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_file():
            # Destination is a pre-created empty placeholder file (e.g. for a bind mount).
            # Write content in-place so the inode stays the same — the bind mount is
            # tied to the inode, so unlinking and re-creating would break it.
            dst.write_bytes(src.read_bytes())
        else:
            dst.hardlink_to(src)
    elif src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for entry in os.scandir(src):
            dst_path = dst / entry.name
            if entry.is_symlink():
                os.symlink(os.readlink(entry.path), dst_path)
            elif entry.is_dir(follow_symlinks=False):
                hardlink_tree(Path(entry.path), dst_path)
            elif entry.is_file(follow_symlinks=False):
                dst_path.hardlink_to(entry.path)


def hardlink_closure(store_paths: set[Path], dst: Path) -> None:
    """
    Hardlink multiple store paths into dst.

    store_paths: {/nix/store/abc-foo, /nix/store/def-bar, ...}
    dst: volume_root/nix/store
    result: dst/abc-foo/..., dst/def-bar/...
    """
    try:
        dst.mkdir(parents=True, exist_ok=True)

        for store_path in store_paths:
            target = dst / store_path.name
            if target.exists():
                continue  # already copied (deduplication across volumes)
            try:
                hardlink_tree(store_path, target)
            except Exception as e:
                raise HardlinkClosureError(
                    f"Failed to hardlink {store_path.name}",
                    logs=str(e),
                ) from e
    except HardlinkClosureError:
        raise
    except Exception as e:
        raise HardlinkClosureError(
            "Failed to hardlink store paths to volume",
            logs=str(e),
        ) from e


def deref_hardlink_tree(src: Path, dst: Path) -> None:
    """
    Recursively copy src to dst, dereferencing symlinks and
    hardlinking files for space efficiency.

    Symlink handling:
    - Symlinks to /nix/store that exist: dereference recursively
    - Symlinks to /nix/store that are broken: log warning and copy as-is
    - Symlinks to outside /nix/store: log warning and copy as-is
    """
    src = Path(src)

    if src.is_symlink():
        target = os.readlink(src)
        resolved = (src.parent / target).resolve()

        # Check if target is outside /nix/store
        try:
            resolved.relative_to("/nix/store")
            in_store = True
        except ValueError:
            in_store = False

        if not in_store:
            # Target is outside /nix/store, log warning and copy symlink as-is
            logger.warning(f"Symlink points outside /nix/store: {src} -> {target}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, dst)
        elif not resolved.exists():
            # Target is in /nix/store but broken, log warning and copy as-is
            logger.warning(f"Broken symlink in /nix/store: {src} -> {target}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, dst)
        else:
            # Target is in /nix/store and exists, dereference it
            deref_hardlink_tree(resolved, dst)
    elif src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.hardlink_to(src)
    elif src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for entry in os.scandir(src):
            dst_path = dst / entry.name
            deref_hardlink_tree(Path(entry.path), dst_path)
