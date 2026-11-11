"""`Hash::parseAny`, every spelling Nix prints a hash in."""

import hashlib

import pytest

from huggorm_bindings import Hash, HashAlgorithm, parse_hash
from huggorm_bindings.errors import NixError

DIGEST = hashlib.sha256(b"hello\n").digest()
SHA256 = Hash(HashAlgorithm.SHA256, DIGEST)


@pytest.mark.parametrize("text", [SHA256.sri(), SHA256.to_string(), f"sha256:{DIGEST.hex()}", DIGEST.hex()])
def test_each_spelling_of_one_hash_parses_to_it(text: str) -> None:
    assert parse_hash(text) == SHA256


def test_a_bare_digest_is_read_as_the_algorithm_given() -> None:
    digest = hashlib.sha1(b"x").digest()
    assert parse_hash(digest.hex(), HashAlgorithm.SHA1) == Hash(HashAlgorithm.SHA1, digest)


def test_a_prefix_that_names_another_algorithm_is_refused() -> None:
    with pytest.raises(NixError, match="sha512"):
        parse_hash(SHA256.sri(), HashAlgorithm.SHA512)
