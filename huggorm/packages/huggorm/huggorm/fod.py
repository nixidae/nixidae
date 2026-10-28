"""Find a fixed-output hash in a Nix file, and replace it with the one Nix got.

Needs the `fod` extra: tree-sitter and tree-sitter-nix. Nothing else in
huggorm imports this module, so huggorm without the extra still loads.

The mismatch comes from Nix's two-line diagnostic, because no structured
API reports it. Every step refuses rather than guesses: two mismatches,
two candidate bindings, or a mismatch outside the target's closure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import tree_sitter_nix  # type: ignore[import-untyped] -- tree-sitter-nix ships no stubs
from tree_sitter import Language, Parser

from huggorm_bindings import DerivationOutputCAFixed, StorePath, filter_ansi_escapes

if TYPE_CHECKING:
    from collections.abc import Iterable

    from huggorm_generated import AsyncDerivation, AsyncEvalState, AsyncStore, AsyncValue

_HASH_ATTRIBUTES = frozenset({"hash", "sha256", "outputHash"})
_HASH = r"(?:md5|sha1|sha256|sha512)(?::[0-9a-z]+|-[A-Za-z0-9+/]+={0,2})"
_FOD_MISMATCH = re.compile(
    r"^\s*(?:error:\s+)?hash mismatch in "
    r"(?:fixed-output derivation '(?P<drv_path>[^']+)'|file downloaded from '[^']+'):\n"
    rf"\s*specified:\s+(?P<specified>{_HASH})\n"
    rf"\s*got:\s+(?P<got>{_HASH})$",
    re.MULTILINE,
)
# tree-sitter-nix 0.3 exposes the legacy integer language pointer.
_NIX_LANGUAGE = Language(cast("int", tree_sitter_nix.language()))

_APPLY_EXPRESSION_ARITY = 2

# An author writes `lib.fakeHash` for "not computed yet". Nix reports the
# SRI form of the constant as `specified`, so a symbol matches on value as
# a literal does. `fakeHash` and `fakeSha256` are the same 32 zero bytes;
# checked against `nix hash convert`.
_FAKE_HASH_SYMBOLS = {
    "fakeHash": "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "fakeSha256": "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "fakeSha512": "sha512-" + "A" * 86 + "==",
}

# Plain identifiers only: `lib.fakeHash or "x"` no longer names the constant.
_SYMBOL_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_'-]*(?:\.[A-Za-z_][A-Za-z0-9_'-]*)*")

_SYMBOL_EXPRESSIONS = frozenset({"variable_expression", "select_expression"})

# `binding` is `attrpath "=" expression ";"`.
_BINDING_NAMED_CHILDREN = 2


class FodSourceUpdateError(ValueError):
    """A fixed-output mismatch does not map to one safe source binding."""


@dataclass(frozen=True, slots=True)
class FodHashMismatch:
    """The specified and the actual hash of one fixed-output mismatch."""

    specified: str
    got: str
    drv_path: str | None


@dataclass(frozen=True, slots=True)
class FodHashLiteral:
    """One hash binding in a Nix file.

    `value` is the hash it names: the text of a string literal, or the
    constant a `lib.fakeHash`-style symbol stands for. The byte span covers
    the whole value node, so a replacement is one splice for both shapes.
    """

    attribute: str
    value: str
    start_byte: int
    end_byte: int
    derivation_name: str | None = None


def extract_fod_hash_mismatch(message: str) -> FodHashMismatch | None:
    """The mismatch in `message`, only when it holds exactly one."""
    matches = list(_FOD_MISMATCH.finditer(filter_ansi_escapes(message, filter_all=True)))
    if len(matches) != 1:
        return None
    match = matches[0]
    return FodHashMismatch(specified=match["specified"], got=match["got"], drv_path=match["drv_path"])


def extract_unique_fod_hash_mismatch(messages: Iterable[str]) -> FodHashMismatch | None:
    """The one mismatch in `messages`; none when a derivation reports two."""
    by_drv: dict[str | None, list[FodHashMismatch]] = {}
    for message in messages:
        mismatch = extract_fod_hash_mismatch(message)
        if mismatch is not None:
            by_drv.setdefault(mismatch.drv_path, []).append(mismatch)
    unambiguous: list[FodHashMismatch] = []
    for items in by_drv.values():
        first = items[0]
        if any(item != first for item in items[1:]):
            return None
        unambiguous.append(first)
    return unambiguous[0] if unambiguous else None


def find_fod_hash_literal(source: str, specified: str, *, derivation_name: str | None = None) -> FodHashLiteral:
    """The one hash binding in `source` that produced the failed derivation."""
    candidates = _fod_hash_literals(source.encode())
    exact = [candidate for candidate in candidates if candidate.value == specified]
    if len(exact) == 1:
        return exact[0]
    if derivation_name is not None:
        named = [c for c in candidates if c.derivation_name == derivation_name]
        if len(named) == 1:
            return named[0]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise FodSourceUpdateError(
            "no hash, sha256, or outputHash binding found that is a plain string or a fakeHash symbol"
        )
    raise FodSourceUpdateError(
        "multiple hash literals found; refusing to guess which one produced the failed derivation"
    )


def replace_fod_hash(source: str, literal: FodHashLiteral, got: str) -> str:
    """`source` with the value of `literal` replaced by the quoted `got`."""
    if '"' in got or "\n" in got:
        raise FodSourceUpdateError("the computed hash is not safe to insert into a Nix string literal")
    encoded = source.encode()
    return (encoded[: literal.start_byte] + f'"{got}"'.encode() + encoded[literal.end_byte :]).decode()


def derivation_name_from_path(drv_path: str | None) -> str | None:
    """The derivation name in a `.drv` path, or none for any other string."""
    if drv_path is None:
        return None
    base_name = drv_path.rsplit("/", 1)[-1]
    hash_separator = base_name.find("-")
    if hash_separator < 0 or not base_name.endswith(".drv"):
        return None
    return base_name[hash_separator + 1 : -len(".drv")]


async def _is_fixed_output(derivation: AsyncDerivation) -> bool:
    outputs = await derivation.outputs()
    return any(isinstance(output, DerivationOutputCAFixed) for output in outputs.values())


async def evaluated_derivation_path(state: AsyncEvalState, value: AsyncValue) -> StorePath | None:
    """The `.drv` of `value` when it is a derivation, and none otherwise."""
    await state.force(value)
    if await value.type_name() != "attrs" or not await value.has("type"):
        return None
    kind = await value.get("type")
    await state.force(kind)
    if await kind.type_name() != "string" or await kind.string_value() != "derivation":
        return None
    if not await value.has("drvPath"):
        return None
    return await value.drv_path()


async def is_fixed_output_derivation_in_closure(store: AsyncStore, root: StorePath, candidate: StorePath) -> bool:
    """Whether `candidate` is a fixed-output member of `root`'s closure.

    Nix builds a resolved copy of a FOD whose hash is empty, under a new
    path. So a name match counts too, when exactly one fixed-output
    member carries that name.
    """
    candidate_name = derivation_name_from_path(candidate.to_string())
    pending = [root]
    visited: set[StorePath] = set()
    name_matches = 0
    while pending:
        drv_path = pending.pop()
        if drv_path in visited:
            continue
        visited.add(drv_path)
        derivation = await store.read_derivation(drv_path)
        if drv_path == candidate:
            return await _is_fixed_output(derivation)
        if candidate_name is not None and await derivation.name() == candidate_name:
            if not await _is_fixed_output(derivation):
                return False
            name_matches += 1
        pending.extend([StorePath(base) for base in await derivation.input_drvs()])
    return name_matches == 1


async def mismatch_is_target_fod(
    store: AsyncStore, state: AsyncEvalState, root: AsyncValue, mismatch: FodHashMismatch
) -> bool:
    """Whether `mismatch` belongs to the evaluated target `root`.

    `builtins.fetchurl` names no derivation, so its mismatch is taken on
    the diagnostic alone. Any other must name a fixed-output derivation in
    the target's closure before a source file changes.
    """
    if mismatch.drv_path is None:
        return True
    root_drv = await evaluated_derivation_path(state, root)
    if root_drv is None:
        return False
    # A target that is itself a FOD with an empty hash builds as a
    # resolved copy, under a path other than its own `drvPath`.
    if await _is_fixed_output(await store.read_derivation(root_drv)):
        return True
    candidate = await store.parse_store_path(mismatch.drv_path)
    return await is_fixed_output_derivation_in_closure(store, root_drv, candidate)


def _fod_hash_literals(source: bytes) -> list[FodHashLiteral]:
    root = Parser(_NIX_LANGUAGE).parse(source).root_node
    run_command_bindings = _run_command_hash_bindings(root, source)
    literals: list[FodHashLiteral] = []
    nodes: list[Any] = [root]
    while nodes:
        node = nodes.pop()
        nodes.extend(reversed(node.children))
        if node.type != "binding":
            continue
        named = node.named_children
        if len(named) != _BINDING_NAMED_CHILDREN or named[0].type != "attrpath":
            continue
        attrpath, value_node = named
        attribute = source[attrpath.start_byte : attrpath.end_byte].decode()
        if attribute not in _HASH_ATTRIBUTES:
            continue
        value = _hash_binding_value(value_node, source)
        if value is None:
            continue
        literals.append(
            FodHashLiteral(
                attribute,
                value,
                value_node.start_byte,
                value_node.end_byte,
                run_command_bindings.get(node.start_byte),
            ),
        )
    return literals


def _hash_binding_value(node: Any, source: bytes) -> str | None:
    """The hash a literal or a fake-hash symbol names; none for any other shape."""
    rendered = source[node.start_byte : node.end_byte]
    if node.type == "string_expression":
        if any(child.type == "interpolation" for child in node.children):
            return None
        if not rendered.startswith(b'"') or not rendered.endswith(b'"'):
            return None
        return rendered[1:-1].decode()
    if node.type in _SYMBOL_EXPRESSIONS:
        text = rendered.decode()
        if not _SYMBOL_PATH.fullmatch(text):
            return None
        return _FAKE_HASH_SYMBOLS.get(text.rsplit(".", 1)[-1])
    return None


def _run_command_name(node: Any, source: bytes) -> tuple[str, Any] | None:
    """The name and attribute set of a `runCommand NAME { ... }` call."""
    if node.type != "apply_expression" or len(node.named_children) != _APPLY_EXPRESSION_ARITY:
        return None
    function, attrs = node.named_children
    if attrs.type != "attrset_expression" or function.type != "apply_expression":
        return None
    if len(function.named_children) != _APPLY_EXPRESSION_ARITY:
        return None
    callee, name = function.named_children
    if name.type != "string_expression" or any(child.type == "interpolation" for child in name.children):
        return None
    rendered = source[name.start_byte : name.end_byte]
    if (
        not source[callee.start_byte : callee.end_byte].endswith(b"runCommand")
        or not rendered.startswith(b'"')
        or not rendered.endswith(b'"')
    ):
        return None
    return rendered[1:-1].decode(), attrs


def _run_command_hash_bindings(root: Any, source: bytes) -> dict[int, str]:
    """Each `outputHash` binding of a `runCommand NAME { ... }`, by start byte."""
    names: dict[int, str] = {}
    nodes: list[Any] = [root]
    while nodes:
        node = nodes.pop()
        nodes.extend(reversed(node.children))
        call = _run_command_name(node, source)
        if call is None:
            continue
        derivation_name, attrs = call
        binding_set = next((child for child in attrs.named_children if child.type == "binding_set"), None)
        if binding_set is None:
            continue
        for binding in binding_set.named_children:
            if binding.type != "binding":
                continue
            attrpath = next((child for child in binding.named_children if child.type == "attrpath"), None)
            if attrpath is not None and source[attrpath.start_byte : attrpath.end_byte] == b"outputHash":
                names[binding.start_byte] = derivation_name
    return names
