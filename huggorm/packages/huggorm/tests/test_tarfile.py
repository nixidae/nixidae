"""Unpacking an archive with Nix's libarchive reader."""

import io
import pathlib
import tarfile
import zipfile

import pytest

from huggorm_bindings import unpack_tarfile
from huggorm_bindings.errors import NixError


def test_a_compressed_tarball_unpacks_its_tree(tmp_path: pathlib.Path) -> None:
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        data = b"hello\n"
        info = tarfile.TarInfo("top/sub/file")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    destination = tmp_path / "out"
    destination.mkdir()
    unpack_tarfile(archive, destination)
    assert (destination / "top" / "sub" / "file").read_bytes() == b"hello\n"


def test_a_zip_file_unpacks_too(tmp_path: pathlib.Path) -> None:
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("one", "1")
        zipped.writestr("two", "2")
    destination = tmp_path / "out"
    destination.mkdir()
    unpack_tarfile(archive, destination)
    assert sorted(p.name for p in destination.iterdir()) == ["one", "two"]


def test_a_file_that_is_no_archive_is_refused(tmp_path: pathlib.Path) -> None:
    archive = tmp_path / "plain"
    archive.write_bytes(b"not an archive")
    destination = tmp_path / "out"
    destination.mkdir()
    with pytest.raises(NixError):
        unpack_tarfile(archive, destination)
