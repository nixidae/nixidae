"""The placeholders that stand for an output path before it exists."""

from __future__ import annotations

import base64
import hashlib
import posixpath

#: Nix's base-32 alphabet. It is not RFC 4648.
NIX32_ALPHABET = "0123456789abcdfghijklmnpqrsvwxyz"


def nix32(digest: bytes) -> str:
    """Encode ``digest`` as Nix prints a hash: most significant bits first."""
    length = (len(digest) * 8 - 1) // 5 + 1
    chars: list[str] = []
    for n in range(length - 1, -1, -1):
        byte, offset = divmod(n * 5, 8)
        value = digest[byte] >> offset
        if byte + 1 < len(digest):
            value |= digest[byte + 1] << (8 - offset)
        chars.append(NIX32_ALPHABET[value & 0x1F])
    return "".join(chars)


def output_path_name(drv_name: str, output: str) -> str:
    """``outputPathName``: ``out`` takes the derivation's name, others a suffix."""
    return drv_name if output == "out" else f"{drv_name}-{output}"


def placeholder(output: str = "out") -> str:
    """What a floating output of the derivation itself is called in its env.

    ``builtins.placeholder``. The same string for every derivation.
    """
    return "/" + nix32(hashlib.sha256(f"nix-output:{output}".encode()).digest())


def downstream_placeholder(drv_path: str, output: str = "out") -> str:
    """What another derivation writes for a floating output of ``drv_path``.

    ``DownstreamPlaceholder::unknownCaOutput``. Nix rewrites it to the real
    path once ``drv_path`` is built.
    """
    base = posixpath.basename(drv_path)
    hash_part, _, name = base.partition("-")
    drv_name = name.removesuffix(".drv")
    clear = f"nix-upstream-output:{hash_part}:{output_path_name(drv_name, output)}"
    return "/" + nix32(hashlib.sha256(clear.encode()).digest())


def dynamic_placeholder(drv_path: str, output: str = "out", inner: str = "out") -> str:
    """What another derivation writes for output ``output`` of the derivation
    that output ``inner`` of ``drv_path`` is: ``builtins.outputOf``.

    ``DownstreamPlaceholder::unknownDerivation``.
    """
    base = posixpath.basename(drv_path)
    hash_part, _, name = base.partition("-")
    drv_name = name.removesuffix(".drv")
    upstream = f"nix-upstream-output:{hash_part}:{output_path_name(drv_name, inner)}"
    clear = f"nix-computed-output:{nix32(_fold(hashlib.sha256(upstream.encode()).digest()))}:{output}"
    return "/" + nix32(hashlib.sha256(clear.encode()).digest())


def _fold(digest: bytes, size: int = 20) -> bytes:
    folded = bytearray(size)
    for index, byte in enumerate(digest):
        folded[index % size] ^= byte
    return bytes(folded)


def fixed_output_path(name: str, sri: str, method: str = "flat", store: str = "/nix/store") -> str:
    """The path of a fixed output with no references: ``makeFixedOutputPath``."""
    algorithm, _, encoded = sri.partition("-")
    digest = base64.b64decode(encoded)
    if method == "nar" and algorithm == "sha256":
        kind, inner = "source", digest
    else:
        prefix = "r:" if method == "nar" else ""
        kind = "output:out"
        inner = hashlib.sha256(f"fixed:out:{prefix}{algorithm}:{digest.hex()}:".encode()).digest()
    clear = f"{kind}:sha256:{inner.hex()}:{store}:{name}"
    return f"{store}/{nix32(_fold(hashlib.sha256(clear.encode()).digest()))}-{name}"
