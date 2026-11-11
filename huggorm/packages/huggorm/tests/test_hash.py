"""`Hash::parseAny`, every spelling Nix prints a hash in, and `nix hash path`."""

import hashlib
import os
from pathlib import Path

import pytest

from huggorm_bindings import (
    FileIngestionMethod,
    Hash,
    HashAlgorithm,
    HashFormat,
    hash_path,
    parse_hash,
    parse_hash_format,
)
from huggorm_bindings.errors import NixError

DIGEST = hashlib.sha256(b"hello\n").digest()
SHA256 = Hash(HashAlgorithm.SHA256, DIGEST)


@pytest.mark.parametrize("text", [SHA256.sri(), SHA256.to_string(), f"sha256:{DIGEST.hex()}"])
def test_each_spelling_of_one_hash_parses_to_it(text: str) -> None:
    assert parse_hash(text) == SHA256


def test_a_bare_digest_is_read_as_the_algorithm_given() -> None:
    digest = hashlib.sha1(b"x").digest()
    assert parse_hash(digest.hex(), HashAlgorithm.SHA1) == Hash(HashAlgorithm.SHA1, digest)


def test_a_bare_digest_without_an_algorithm_is_refused() -> None:
    with pytest.raises(NixError, match="does not include a type"):
        parse_hash(DIGEST.hex())


def test_a_prefix_that_names_another_algorithm_is_refused() -> None:
    with pytest.raises(NixError, match="sha512"):
        parse_hash(SHA256.sri(), HashAlgorithm.SHA512)


@pytest.mark.parametrize("format", list(HashFormat))
def test_each_rendering_parses_back_in_its_format(format: HashFormat) -> None:
    text = SHA256.render(format)
    assert parse_hash(text, HashAlgorithm.SHA256) == SHA256
    assert parse_hash_format(text, HashAlgorithm.SHA256) == format


def test_only_sri_carries_the_algorithm() -> None:
    assert SHA256.render(HashFormat.SRI) == SHA256.sri()
    assert SHA256.render(HashFormat.BASE16) == DIGEST.hex()


def test_a_flat_hash_is_the_digest_of_the_contents(tmp_path: Path) -> None:
    file = tmp_path / "f"
    file.write_bytes(b"hello\n")
    os.symlink(file, tmp_path / "link")
    assert hash_path(file, FileIngestionMethod.FLAT, HashAlgorithm.SHA256) == SHA256
    assert hash_path(tmp_path / "link", FileIngestionMethod.FLAT, HashAlgorithm.SHA256) == SHA256


def test_a_nar_hash_differs_from_the_flat_one(tmp_path: Path) -> None:
    file = tmp_path / "f"
    file.write_bytes(b"hello\n")
    nar = hash_path(file, FileIngestionMethod.NAR, HashAlgorithm.SHA256)
    assert nar.algorithm() == HashAlgorithm.SHA256
    assert nar != SHA256
