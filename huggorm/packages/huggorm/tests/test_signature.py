"""Signing keys and store path signatures: `nix key` and `nix store sign`.

A path is signed as `ValidPathInfo::sign` signs it: the secret key signs
`Store.fingerprint` of the path info, and `Store.add_signatures` keeps it.
"""

from __future__ import annotations

import copy
import pathlib
from collections.abc import Iterator

import pytest
from nixversion import EXPERIMENTAL_FEATURE

from huggorm_bindings import (
    ContentAddressMethod,
    HashAlgorithm,
    KeyType,
    PublicKey,
    SecretKey,
    Signature,
    Store,
    generate_secret_key,
    get_setting,
    set_setting,
)
from huggorm_bindings.errors import NixError
from huggorm_dsl.declare import DETERMINATE

#: Every key type Determinate Nix 3.24 names (`getKeyTypes()`).
_DETERMINATE_KEY_TYPES = ("ed25519", "ml-dsa-44", "ml-dsa-65", "ml-dsa-87", "ecdsa-p384")


@pytest.fixture(params=["local", "daemon"])
def signing_store(request: pytest.FixtureRequest, tmp_path: pathlib.Path) -> Store:
    """A LocalStore, and a RemoteStore over a daemon of this lane's Nix: the
    two `addSignatures` that keep a cached path info."""
    if request.param == "daemon":
        return Store(request.getfixturevalue("private_daemon"))
    return Store(str(tmp_path / "store"))


def test_a_secret_key_reads_its_own_text_back() -> None:
    key = generate_secret_key("cache-1")
    assert key.name() == "cache-1"
    assert key.to_string().startswith("cache-1:")
    assert SecretKey(key.to_string()) == key


def test_the_public_key_has_the_name_of_the_secret_key() -> None:
    public = generate_secret_key("cache-1").to_public_key()
    assert public.name() == "cache-1"
    assert PublicKey(public.to_string()) == public
    assert str(public) == public.to_string()


def test_a_repr_does_not_print_the_secret() -> None:
    key = generate_secret_key("cache-1")
    secret = key.to_string().split(":", 1)[1]
    assert secret not in repr(key)
    assert "cache-1" in repr(key)


@pytest.mark.parametrize("text", ["cache-1", "cache-1:not-base64!", ":"])
def test_a_corrupt_key_is_refused(text: str) -> None:
    with pytest.raises(NixError):
        SecretKey(text)
    with pytest.raises(NixError):
        PublicKey(text)


def test_a_signature_verifies_with_its_own_key_only() -> None:
    key = generate_secret_key("cache-1")
    sig = key.sign_detached("data")
    assert sig.key_name() == "cache-1"
    assert key.to_public_key().verify_detached("data", sig)
    assert not key.to_public_key().verify_detached("other data", sig)
    # Same name, another key: Nix checks the bytes, not only the name.
    assert not generate_secret_key("cache-1").to_public_key().verify_detached("data", sig)


def test_a_copied_key_still_signs_and_verifies() -> None:
    """A Determinate key is one class per key type. A copy of the C++
    base would keep the bytes and lose the type, and then throw."""
    key = copy.copy(generate_secret_key("cache-1"))
    sig = copy.deepcopy(key).sign_detached("data")
    assert copy.copy(key.to_public_key()).verify_detached("data", sig)


def test_every_nix_has_ed25519_and_makes_it_by_default() -> None:
    """`list(KeyType)` is how a caller asks a Nix which key types it has."""
    assert KeyType.ED25519 in list(KeyType)
    assert generate_secret_key("cache-1", KeyType.ED25519).to_public_key().name() == "cache-1"


@pytest.mark.parametrize("word", ["no-such-type", *(w for w in _DETERMINATE_KEY_TYPES if w not in set(KeyType))])
def test_a_key_type_this_nix_lacks_is_refused(word: str) -> None:
    """NixOS/nix has no parser for a key type. Without the emitted check a
    body that ignores the word made an ed25519 key from `ml-dsa-44`."""
    with pytest.raises(NixError, match=word):
        generate_secret_key("cache-1", word)  # type: ignore[arg-type]  # a word KeyType lacks is the case under test


if DETERMINATE:
    # More than one key type is Determinate Nix's. mypy.ini states the
    # lane the tree is checked against, so this block is not checked.

    @pytest.fixture
    def cnsa() -> Iterator[None]:
        """The `cnsa` feature, which every key type but ed25519 needs, on
        for one test. The setting belongs to the process."""
        before = get_setting("experimental-features") or ""
        set_setting("extra-experimental-features", "cnsa")
        try:
            yield
        finally:
            set_setting("experimental-features", before)

    @pytest.mark.parametrize("key_type", [t for t in KeyType if t is not KeyType.ED25519])
    def test_a_key_type_past_ed25519_needs_cnsa(key_type: KeyType) -> None:
        with pytest.raises(NixError, match=EXPERIMENTAL_FEATURE):
            generate_secret_key("cache-1", key_type)

    @pytest.mark.parametrize("key_type", list(KeyType))
    def test_every_key_type_signs_and_parses_back(key_type: KeyType, cnsa: None) -> None:
        key = generate_secret_key("cache-1", key_type)
        sig = key.sign_detached("data")
        public = PublicKey(key.to_public_key().to_string())
        assert public.verify_detached("data", sig)
        assert SecretKey(key.to_string()).sign_detached("other").key_name() == "cache-1"

    @pytest.mark.parametrize("key_type", [t for t in KeyType if t is not KeyType.ED25519])
    def test_an_openssl_key_has_a_pem_form(key_type: KeyType, cnsa: None) -> None:
        key = generate_secret_key("cache-1", key_type)
        assert key.to_pem().startswith("-----BEGIN PRIVATE KEY-----\n")
        assert key.to_public_key().to_pem().startswith("-----BEGIN PUBLIC KEY-----\n")


def test_an_ed25519_key_has_no_pem_form_on_any_nix() -> None:
    """Determinate Nix converts only its OpenSSL keys, and NixOS/nix has
    ed25519 keys alone, so both refuse with Determinate Nix's words."""
    key = generate_secret_key("cache-1")
    with pytest.raises(NixError, match="conversion to PEM is not implemented for this key type"):
        key.to_pem()
    with pytest.raises(NixError, match="conversion to PEM is not implemented for this key type"):
        key.to_public_key().to_pem()


def test_a_signed_path_keeps_the_signature(signing_store: Store) -> None:
    """The same Store answers the new signature: `addSignatures` drops its
    cached path info (`nix-add-signatures-invalidates-path-info.patch`)."""
    store = signing_store
    path = store.add_to_store("greeting", b"hello\n", ContentAddressMethod.TEXT, HashAlgorithm.SHA256)
    info = store.query_path_info(path)
    assert info.sigs() == []
    fingerprint = store.fingerprint(info)
    assert fingerprint.startswith(f"1;{store.print_store_path(path)};sha256:")

    key = generate_secret_key("cache-1")
    sig = key.sign_detached(fingerprint)
    store.add_signatures(path, [sig])
    store.add_signatures(path, [sig])

    signed = store.query_path_info(path)
    assert signed.sigs() == [sig]
    assert key.to_public_key().verify_detached(store.fingerprint(signed), signed.sigs()[0])


@pytest.fixture
def trusted_key() -> Iterator[SecretKey]:
    """A key whose public half is the one `trusted-public-keys` names, for
    one test. The setting belongs to the process."""
    key = generate_secret_key("cache-1")
    before = get_setting("trusted-public-keys") or ""
    set_setting("trusted-public-keys", key.to_public_key().to_string())
    try:
        yield key
    finally:
        set_setting("trusted-public-keys", before)


def test_a_signature_checks_against_the_trusted_keys(signing_store: Store, trusted_key: SecretKey) -> None:
    store = signing_store
    path = store.add_to_store("greeting", b"hello\n", ContentAddressMethod.TEXT, HashAlgorithm.SHA256)
    info = store.query_path_info(path)
    assert store.check_signature(info, trusted_key.sign_detached(store.fingerprint(info)))
    assert not store.check_signature(info, trusted_key.sign_detached("other data"))
    # Same name, another key.
    assert not store.check_signature(info, generate_secret_key("cache-1").sign_detached(store.fingerprint(info)))


def test_a_path_is_content_addressed_in_its_own_store_directory_only(tmp_path: pathlib.Path) -> None:
    store = Store(str(tmp_path / "store"))
    path = store.add_to_store("greeting", b"hello\n", ContentAddressMethod.TEXT, HashAlgorithm.SHA256)
    info = store.query_path_info(path)
    assert store.is_content_addressed(info)
    assert not Store("dummy://?store=/other/store").is_content_addressed(info)


def test_the_nar_hash_of_a_path_is_the_one_the_store_registered(tmp_path: pathlib.Path) -> None:
    """A local store only: a `unix://` store reads the NAR from this host's
    `/nix/store` (`LocalFSStore::narFromPath`), not from the daemon's chroot."""
    store = Store(str(tmp_path / "store"))
    path = store.add_to_store("greeting", b"hello\n", ContentAddressMethod.TEXT, HashAlgorithm.SHA256)
    info = store.query_path_info(path)
    assert store.compute_nar_hash(path, info.nar_hash().algorithm()) == info.nar_hash()
    assert store.compute_nar_hash(path, HashAlgorithm.SHA512) != info.nar_hash()


def test_a_store_that_cannot_keep_signatures_says_so() -> None:
    store = Store("dummy://")
    with pytest.raises(NixError):
        store.add_signatures(
            store.parse_store_path("/nix/store/7rjjfrn5w3z1kb2v9v0ilxmvmb2n5k1y-hello-2.12.1"),
            [Signature("k", bytes(64))],
        )
