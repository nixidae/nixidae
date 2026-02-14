# SPDX-License-Identifier: MIT

import os
from pathlib import Path

from .errors import HardlinkClosureError


def hardlink_tree(src: Path, dst: Path) -> None:
    """Hardlink a single path (file or directory tree), preserving symlinks."""
    if src.is_symlink():
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(os.readlink(src), dst)
    elif src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
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


def hardlink_closure(store_paths: list[Path], dst: Path) -> None:
    """
    Hardlink multiple store paths into dst.

    store_paths: [/nix/store/abc-foo, /nix/store/def-bar, ...]
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
    All symlink targets must exist on the same filesystem as dst.
    """
    resolved = Path(src).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Broken symlink: {src} -> {resolved}")

    if resolved.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.hardlink_to(resolved)
    elif resolved.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for entry in os.scandir(resolved):
            dst_path = dst / entry.name
            deref_hardlink_tree(Path(entry.path), dst_path)
