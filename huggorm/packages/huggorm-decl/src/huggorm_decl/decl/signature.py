"""
nix::Signature, nix::PublicKey and nix::SecretKey:
`nix/util/signature/local-keys.hh`.

A signature is a MESSAGE rather than a string, for the reason
`decl/hash.py` gives. `<key-name>:<base64>` is one rendering of two
facts - who vouched, and what they wrote - and a caller who wants to
know WHICH key signed a path should not be splitting a string to find
out.

A key is the opposite case: its text is the only form Nix reads or
writes, and its constructor is the parser, so a key crosses as text.

Determinate Nix makes a key polymorphic: one class per algorithm, made by
`parse` and `generate` as a `unique_ptr`, and no public constructor. So
there Python holds a key through a `shared_ptr`, and a factory opens one.
The surface is the same, plus the `KeyType` that `generate` takes.
"""

from huggorm_decl.decl.words import KeyType
from huggorm_dsl.declare import (
    DETERMINATE,
    Bint,
    Bytes,
    Cxx,
    Field,
    Local,
    Str,
    binding,
    constructs,
    header,
    instant,
    local,
    needs,
    reads,
    threading,
    wire_value,
)


@header(Local("nix/util/signature/local-keys.hh"))
@binding(
    cxx="nix::Signature",
    # Two strings the object already owns.
    threading="pool",
    blocking=False,
)
@wire_value(
    # nix::Signature defaults operator<=>, so the binding declares the
    # comparisons rather than comparing parts in Python.
    compare="cxx",
    order=True,
    text="to_string",
)
class Signature:
    """Who vouched for a store path, and what they wrote.

    A signature says a path came from somewhere and arrived intact, so
    both halves matter: the key name is what a caller checks against
    their trusted keys, and the bytes are what verifies.
    """

    def __init__(self, key_name: Str, sig: Bytes) -> None:
        """Build one from its parts.

        No validation, because there is nothing here to validate: a
        signature is checked against a public key, which is a question
        for the store rather than for the constructor."""
        Cxx("new (self) nix::Signature{key_name, std::string(sig.c_str(), sig.size())};")

    # WIRE ORDER: the two facts, in the order the rendering states them.

    @reads("keyName")
    def key_name(self) -> Str:
        """Which key signed it - `cache.nixos.org-1`, usually."""

    @reads("sig")
    def sig(self) -> Bytes:
        """The raw signature bytes, decoded.

        Upstream keeps them decoded and renders base64 on the way out,
        so this is the shorter path as well as the honest one."""

    # --- the rendering, which is derived from both fields above ------

    @local
    @instant
    def to_string(self) -> Str:
        """`<key-name>:<base64>` - what a .narinfo carries and what
        `nix path-info --sigs` prints."""


@header(Local("nix/util/signature/local-keys.hh"))
@binding(
    cxx="nix::PublicKey",
    # Two strings the object owns, and libsodium over them.
    threading="pool",
    blocking=False,
    holder="shared_ptr" if DETERMINATE else "",
)
@wire_value(
    # The text IS the key: `<name>:<base64>`, which the constructor parses.
    fields=(Field("text", read="to_string"),),
    text="to_string",
)
class PublicKey:
    """A key that verifies signatures, as `trusted-public-keys` lists it."""

    if DETERMINATE:

        def __init__(self) -> None:
            """Parse `<name>:<base64>`. Raises when the key is corrupt.

            `parse_public_key` is the factory, because each key type is
            its own class."""

    else:

        def __init__(self, text: Str) -> None:
            """Parse `<name>:<base64>`. Raises when the key is corrupt."""
            ...

    @reads("name")
    def name(self) -> Str:
        """The key name, which a signature names to say which key made it."""

    @local
    @instant
    def to_string(self) -> Str:
        """`<name>:<base64>`, as `nix key convert-secret-to-public` prints it."""
        ...

    @local
    @instant
    def verify_detached(self, data: Str, sig: Signature) -> Bint:
        """Whether `sig` is this key's signature of `data`. A signature
        of another key name is false."""
        Cxx("return self.verifyDetached(data, sig);")


@header(Local("nix/util/signature/local-keys.hh"))
@binding(
    cxx="nix::SecretKey",
    threading="pool",
    blocking=False,
    holder="shared_ptr" if DETERMINATE else "",
)
@wire_value(
    fields=(Field("text", read="to_string"),),
    # A repr names the key and does not print the secret.
    shown="name",
)
class SecretKey:
    """A key that signs store paths, as `secret-key-files` names one."""

    if DETERMINATE:

        def __init__(self) -> None:
            """Parse `<name>:<base64>`. Raises when the key is corrupt.

            `parse_secret_key` is the factory, because each key type is
            its own class."""

    else:

        def __init__(self, text: Str) -> None:
            """Parse `<name>:<base64>`. Raises when the key is corrupt."""
            ...

    @reads("name")
    def name(self) -> Str:
        """The key name, which every signature of this key carries."""

    @local
    @instant
    def to_string(self) -> Str:
        """`<name>:<base64>`, as `nix key generate-secret` prints it."""
        ...

    @local
    @instant
    def to_public_key(self) -> PublicKey:
        """The public half, which verifies what this key signs."""
        Cxx("return self.toPublicKey();")

    @local
    @instant
    def sign_detached(self, data: Str) -> Signature:
        """The signature of `data`, under this key's name.

        For a store path, `data` is `Store.fingerprint` of its path info,
        as `ValidPathInfo::sign` signs it."""
        Cxx("return self.signDetached(data);")


if DETERMINATE:
    # `parse` answers a `unique_ptr`, and the `shared_ptr` holder takes it.

    @constructs(PublicKey)
    @needs(Local("nix/util/signature/local-keys.hh"))
    @threading("pool")
    def parse_public_key(text: Str) -> PublicKey:
        """Parse `<name>:<base64>` as the key type its bytes say."""
        Cxx("return nix::PublicKey::parse(text);")

    @constructs(SecretKey)
    @needs(Local("nix/util/signature/local-keys.hh"))
    @threading("pool")
    def parse_secret_key(text: Str) -> SecretKey:
        """Parse `<name>:<base64>` as the key type its bytes say."""
        Cxx("return nix::SecretKey::parse(text);")

    @needs(Local("nix/util/signature/local-keys.hh"))
    @threading("pool")
    def generate_secret_key(name: Str, key_type: KeyType = KeyType.ED25519) -> SecretKey:
        """A new random key called `name`, as `nix key generate-secret` makes one."""
        Cxx("return nix::SecretKey::generate(name, key_type);")

else:

    @needs(Local("nix/util/signature/local-keys.hh"))
    @threading("pool")
    def generate_secret_key(name: Str, key_type: KeyType = KeyType.ED25519) -> SecretKey:
        """A new random key called `name`, as `nix key generate-secret` makes one."""
        Cxx("""
            (void) key_type;
            return nix::SecretKey::generate(name);
        """)
