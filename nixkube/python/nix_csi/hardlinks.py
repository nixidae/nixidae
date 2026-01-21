import os
from pathlib import Path


def hardlink_tree(src: Path, dst: Path) -> None:
    """Hardlink a single directory tree, preserving symlinks."""
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
    dst.mkdir(parents=True, exist_ok=True)

    for store_path in store_paths:
        target = dst / store_path.name
        if target.exists():
            continue  # already copied (deduplication across volumes)
        hardlink_tree(store_path, target)


def deref_hardlink_tree(src: Path, dst: Path) -> None:
    """
    Recursively copy src to dst, dereferencing symlinks and
    hardlinking files for space efficiency.

    All symlink targets must exist on the same filesystem as dst.
    """
    src = Path(src)
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)

    for entry in os.scandir(src):
        dst_path = dst / entry.name
        resolved = Path(entry.path).resolve()

        if not resolved.exists():
            raise FileNotFoundError(f"Broken symlink: {entry.path} -> {resolved}")

        if resolved.is_dir():
            deref_hardlink_tree(resolved, dst_path)
        elif resolved.is_file():
            dst_path.hardlink_to(resolved)
