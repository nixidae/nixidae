"""A NAR as a file: `dump_path` writes one, `nar_accessor` reads one.

The NAR `dump_path` writes hashes to what `hash_path` gives for the
same tree, so the two agree on what the serialisation is.
"""

import hashlib
import json
import pathlib

import pytest

from huggorm_bindings import (
    FileIngestionMethod,
    FileType,
    HashAlgorithm,
    Sink,
    dump_path,
    hash_path,
    nar_accessor,
)
from huggorm_bindings.errors import NixError


class Collect(Sink):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.parts.append(data)


@pytest.fixture
def nar(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "f").write_bytes(b"hello\n")
    (root / "x").write_bytes(b"#!/bin/sh\n")
    (root / "x").chmod(0o755)
    (root / "sub" / "z").write_bytes(b"z")
    (root / "l").symlink_to("f")
    sink = Collect()
    dump_path(root, sink)
    data = b"".join(sink.parts)
    assert hashlib.sha256(data).digest() == hash_path(root, FileIngestionMethod.NAR, HashAlgorithm.SHA256).digest()
    out = tmp_path / "tree.nar"
    out.write_bytes(data)
    return out


def test_the_accessor_reads_the_tree_back(nar: pathlib.Path) -> None:
    tree = nar_accessor(nar)
    # Nix's NAR accessor names the entries and leaves their types to lstat.
    assert sorted(tree.read_directory("/")) == ["f", "l", "sub", "x"]
    assert tree.lstat("/sub").type() == FileType.DIRECTORY
    assert tree.read_file("/f") == b"hello\n"
    assert tree.read_link("/l") == "f"
    assert tree.lstat("/x").is_executable()
    assert tree.lstat("/sub/z").file_size() == 1


def test_lstat_raises_nix_s_error_for_a_missing_path(nar: pathlib.Path) -> None:
    with pytest.raises(NixError, match="/nope' does not exist"):
        nar_accessor(nar).lstat("/nope")


def test_the_listing_is_nix_s_json(nar: pathlib.Path) -> None:
    tree = nar_accessor(nar)
    assert json.loads(tree.list_nar("/")) == {
        "type": "directory",
        "entries": {"f": {}, "l": {}, "sub": {}, "x": {}},
    }
    deep = json.loads(tree.list_nar("/sub", recursive=True))
    assert deep["entries"]["z"]["size"] == 1
    assert deep["entries"]["z"]["type"] == "regular"
    assert "narOffset" in deep["entries"]["z"]


def test_a_file_that_is_not_a_nar_is_refused(tmp_path: pathlib.Path) -> None:
    bogus = tmp_path / "bogus.nar"
    bogus.write_bytes(b"not a nar")
    with pytest.raises(NixError):
        nar_accessor(bogus)
