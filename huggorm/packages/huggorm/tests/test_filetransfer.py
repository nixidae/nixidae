"""Downloads through Nix's file transfer, from `file://` URLs so no
test needs a network."""

import pathlib
import stat

import pytest

from huggorm_bindings import download_file
from huggorm_bindings.errors import NixError


def test_a_download_keeps_the_bytes_the_server_sent(tmp_path: pathlib.Path) -> None:
    """No decompression: `prefetchFile` sets `decompress = false`, so a
    gzip stays a gzip and its hash is the hash of the file served."""
    served = tmp_path / "x.gz"
    served.write_bytes(b"\x1f\x8b\x08\x00 not really gzip")
    target = tmp_path / "out"
    download_file(served.as_uri(), target)
    assert target.read_bytes() == served.read_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_an_executable_download_has_mode_0700(tmp_path: pathlib.Path) -> None:
    served = tmp_path / "tool"
    served.write_bytes(b"#!/bin/sh\n")
    target = tmp_path / "out"
    download_file(served.as_uri(), target, executable=True)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_an_existing_file_is_refused(tmp_path: pathlib.Path) -> None:
    served = tmp_path / "x"
    served.write_bytes(b"x")
    target = tmp_path / "out"
    target.write_bytes(b"keep")
    with pytest.raises(NixError, match="creating temporary file"):
        download_file(served.as_uri(), target)
    assert target.read_bytes() == b"keep"


def test_a_missing_file_fails_with_the_transfer_error(tmp_path: pathlib.Path) -> None:
    with pytest.raises(NixError):
        download_file((tmp_path / "absent").as_uri(), tmp_path / "out")
