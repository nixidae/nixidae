from __future__ import annotations

from typing import TYPE_CHECKING

from vivarium_runner.backends.host import make_kept_store

if TYPE_CHECKING:
    from pathlib import Path


def test_no_size_is_no_store(tmp_path: Path) -> None:
    assert make_kept_store(None, tmp_path / "disk", tmp_path) is None
    assert not (tmp_path / "disk").exists()


def test_a_new_store_is_sparse_and_private(tmp_path: Path) -> None:
    disk = make_kept_store(64, tmp_path / "envs" / "lab" / "disks" / "one", tmp_path)
    assert disk is not None
    assert disk.stat().st_size == 64 << 20
    assert disk.stat().st_blocks == 0
    assert disk.stat().st_mode & 0o777 == 0o600
    assert disk.parent.stat().st_mode & 0o777 == 0o700


def test_a_kept_disk_keeps_its_size_and_data(tmp_path: Path) -> None:
    """The guest's ext4 fills the disk it was made on; a larger
    `persistSize` later must not leave it a disk of another size."""
    disk = make_kept_store(1, tmp_path / "one", tmp_path)
    assert disk is not None
    disk.write_bytes(b"ext4" + bytes((1 << 20) - 4))
    assert make_kept_store(64, disk, tmp_path) == disk
    assert disk.stat().st_size == 1 << 20
    assert disk.read_bytes()[:4] == b"ext4"


def test_without_an_environment_it_is_the_runs(tmp_path: Path) -> None:
    assert make_kept_store(1, None, tmp_path) == tmp_path / "kept"


def test_a_container_keeps_a_directory(tmp_path: Path) -> None:
    store = make_kept_store(1, tmp_path / "one", tmp_path, directory=True)
    assert store is not None
    assert store.is_dir()
