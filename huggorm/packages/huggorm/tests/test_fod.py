"""Fixed-output hash updates: the diagnostic, the source, and the closure."""

from __future__ import annotations

import pathlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from huggorm.fod import (
    FodHashMismatch,
    FodSourceUpdateError,
    derivation_name_from_path,
    evaluated_derivation_path,
    extract_fod_hash_mismatch,
    extract_unique_fod_hash_mismatch,
    find_fod_hash_literal,
    is_fixed_output_derivation_in_closure,
    mismatch_is_target_fod,
    replace_fod_hash,
)
from huggorm.session import AsyncSession


def test_extracts_only_the_exact_ansi_colored_nix_fod_shape() -> None:
    mismatch = extract_fod_hash_mismatch(
        "error: hash mismatch in fixed-output derivation "
        "'\x1b[35;1m/nix/store/source.drv\x1b[0m':\n"
        f"  specified: \x1b[35;1m{_FAKE_SRI}\x1b[0m\n"
        f"     got:    \x1b[35;1m{_REAL}\x1b[0m",
    )

    assert mismatch is not None
    assert mismatch.got == _REAL
    assert mismatch.drv_path == "/nix/store/source.drv"
    assert extract_fod_hash_mismatch("hash mismatch in an unrelated format") is None


def test_extracts_through_the_sequences_a_colour_pattern_misses() -> None:
    """Nix's own filter strips every sequence, not only a CSI colour.

    Measured against a CSI-only pattern: an OSC 8 hyperlink stayed around
    `drv_path`, and a 24-bit colour stayed in front of the hash. Nix decides
    what it writes, so a change there must not silently stop the read.
    """
    mismatch = extract_fod_hash_mismatch(
        "error: hash mismatch in fixed-output derivation "
        "'\x1b]8;;https://example.invalid/drv\x1b\\/nix/store/source.drv\x1b]8;;\x1b\\':\n"
        f"  specified: \x1b[38;2;255;0;0m{_FAKE_SRI}\x1b[0m\n"
        f"     got:    \x1b[38;2;0;255;0m{_REAL}\x1b[0m",
    )

    assert mismatch is not None
    assert mismatch.got == _REAL
    assert mismatch.drv_path == "/nix/store/source.drv"


def test_extract_unique_fod_hash_mismatch_rejects_multiple_events() -> None:
    first = (
        "error: hash mismatch in fixed-output derivation '/nix/store/a.drv':\n"
        "  specified: sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n"
        "  got: sha256-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="
    )
    second = first.replace(
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    )

    assert extract_unique_fod_hash_mismatch([first]) == extract_fod_hash_mismatch(first)
    assert extract_unique_fod_hash_mismatch([first, second]) is None


def test_replaces_one_plain_hash_literal() -> None:
    source = 'pkgs.fetchFromGitHub { owner = "lillecarl"; hash = ""; }\n'
    literal = find_fod_hash_literal(source, "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

    assert replace_fod_hash(source, literal, _REAL) == (
        f'pkgs.fetchFromGitHub {{ owner = "lillecarl"; hash = "{_REAL}"; }}\n'
    )


def test_refuses_ambiguous_hash_literals() -> None:
    source = '{ first = { hash = ""; }; second = { sha256 = ""; }; }\n'

    with pytest.raises(FodSourceUpdateError, match="multiple hash literals"):
        find_fod_hash_literal(source, "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")


def test_matches_run_command_output_hash_by_derivation_name() -> None:
    source = """with import <nixpkgs> {};
let
  first = runCommand "first" {
    outputHash = ""; outputHashAlgo = "sha256"; outputHashMode = "flat";
  } "echo first > $out";
  second = runCommand "second" {
    outputHash = ""; outputHashAlgo = "sha256"; outputHashMode = "flat";
  } "echo second > $out";
in [ first second ]
"""

    literal = find_fod_hash_literal(
        source,
        "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        derivation_name="second",
    )

    assert literal.derivation_name == "second"
    assert (
        replace_fod_hash(
            source, literal, "sha256-XG19bBLOoknhsnwV5rVaVGB8DYUiNPMklhyNotZNcD4="
        ).count("sha256-")
        == 1
    )


def test_find_fod_hash_literal_prefers_an_exact_value_match_over_ambiguity() -> None:
    """Two candidates exist, but one's stored value already equals *specified*
    (e.g. it was never actually empty); that one must win outright rather than
    falling through to the "multiple hash literals" ambiguity error."""
    source = f'first = {{ hash = "{_FAKE_SRI}"; }}; second = {{ hash = "sha256-BBBB"; }};'

    literal = find_fod_hash_literal(source, "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

    assert literal.value == "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def test_find_fod_hash_literal_raises_when_derivation_name_matches_nothing() -> None:
    """A derivation_name hint that matches zero (or more than one) candidate
    must fall through to the plain candidate-count check, not silently pick
    one -- here that count is itself ambiguous."""
    source = 'first = { hash = ""; }; second = { hash = ""; };'

    with pytest.raises(FodSourceUpdateError, match="multiple hash literals"):
        find_fod_hash_literal(source, "sha256-nomatch", derivation_name="nonexistent")


def test_find_fod_hash_literal_raises_when_no_candidates_exist() -> None:
    source = '{ pname = "nothing-hash-shaped-here"; }'

    with pytest.raises(FodSourceUpdateError, match="no hash, sha256, or outputHash binding"):
        find_fod_hash_literal(source, "sha256-nomatch")


# `lib.fakeHash` is 32 zero bytes in base64 and `lib.fakeSha256` is the same 32
# bytes in hex, so Nix reports one SRI string for both. `lib.fakeSha512` is 64
# zero bytes. Read out of nixpkgs, and checked against `nix hash convert`.
_FAKE_SRI = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
_FAKE_SHA512_SRI = "sha512-" + "A" * 86 + "=="
_REAL = "sha256-XG19bBLOoknhsnwV5rVaVGB8DYUiNPMklhyNotZNcD4="


@pytest.mark.parametrize(
    ("attribute", "symbol", "specified"),
    [
        ("hash", "lib.fakeHash", _FAKE_SRI),
        ("sha256", "lib.fakeSha256", _FAKE_SRI),
        ("outputHash", "lib.fakeSha512", _FAKE_SHA512_SRI),
        # `with lib;` puts the name in scope on its own.
        ("hash", "fakeHash", _FAKE_SRI),
        # A longer path still ends in the name that carries the constant.
        ("hash", "pkgs.lib.fakeHash", _FAKE_SRI),
    ],
)
def test_updates_a_fake_hash_symbol_as_if_it_were_a_literal(
    attribute: str, symbol: str, specified: str
) -> None:
    """The nixpkgs convention for "not computed yet" is a symbol, not a string.

    An author writes `hash = lib.fakeHash` far more often than the zero string
    it stands for, and the updater saw neither the binding nor a reason to fail
    clearly. Issue #109.
    """
    source = f'fetchurl {{ url = "u"; {attribute} = {symbol}; }}'

    literal = find_fod_hash_literal(source, specified)

    assert literal.value == specified
    assert (
        replace_fod_hash(source, literal, _REAL)
        == f'fetchurl {{ url = "u"; {attribute} = "{_REAL}"; }}'
    )


def test_a_real_hash_elsewhere_survives_an_update_of_a_fake_one() -> None:
    """The acceptance criterion of #109: update the fake, touch nothing else."""
    source = 'a = { hash = lib.fakeHash; }; b = { hash = "sha256-keepmekeepme"; };'

    updated = replace_fod_hash(source, find_fod_hash_literal(source, _FAKE_SRI), _REAL)

    assert updated == f'a = {{ hash = "{_REAL}"; }}; b = {{ hash = "sha256-keepmekeepme"; }};'


def test_two_fake_hash_symbols_are_ambiguous_and_refused() -> None:
    """Both name the same constant, so the reported hash cannot separate them."""
    source = "a = { hash = lib.fakeHash; }; b = { sha256 = fakeSha256; };"

    with pytest.raises(FodSourceUpdateError, match="refusing to guess"):
        find_fod_hash_literal(source, _FAKE_SRI)


@pytest.mark.parametrize(
    "binding",
    [
        # `or` gives the expression a value that is not the constant.
        'hash = lib.fakeHash or "something-else"',
        # A name that is not one of the three says nothing about its value.
        "hash = lib.realHash",
        "hash = myOwnHash",
    ],
)
def test_a_symbol_that_does_not_name_a_known_constant_is_invisible(binding: str) -> None:
    """An unknown symbol has an unknown value, so the updater must not guess."""
    with pytest.raises(FodSourceUpdateError, match="no hash, sha256, or outputHash binding"):
        find_fod_hash_literal(f"x = {{ {binding}; }}", _FAKE_SRI)


def test_replace_fod_hash_rejects_a_computed_hash_with_unsafe_characters() -> None:
    source = 'x = { hash = ""; };'
    literal = find_fod_hash_literal(source, "sha256-nomatch")

    with pytest.raises(FodSourceUpdateError, match="not safe to insert"):
        replace_fod_hash(source, literal, 'sha256-has"quote')
    with pytest.raises(FodSourceUpdateError, match="not safe to insert"):
        replace_fod_hash(source, literal, "sha256-has\nnewline")


def test_find_fod_hash_literal_skips_a_non_runcommand_curried_call() -> None:
    """_run_command_hash_bindings must not attribute a derivation name to a
    curried call whose callee isn't runCommand, even though it has the same
    "NAME { attrs }" apply shape."""
    source = """
    let
      first = mkDerivation "first" { outputHash = ""; };
      second = runCommand "second" { outputHash = ""; } "cmd";
    in [ first second ]
    """

    literal = find_fod_hash_literal(source, "sha256-nomatch", derivation_name="second")

    assert literal.derivation_name == "second"


def test_find_fod_hash_literal_skips_inherit_statements_in_a_runcommand_binding_set() -> None:
    source = """
    let
      second = runCommand "second" { inherit somethingUnrelated; outputHash = ""; } "cmd";
    in second
    """

    literal = find_fod_hash_literal(source, "sha256-nomatch", derivation_name="second")

    assert literal.derivation_name == "second"


def test_derivation_name_from_path_handles_missing_and_malformed_paths() -> None:
    assert derivation_name_from_path(None) is None
    assert derivation_name_from_path("/nix/store/no-drv-suffix") is None
    assert derivation_name_from_path("/nix/store/nodash.drv") is None
    assert derivation_name_from_path("/nix/store/abc123hash-real-name.drv") == "real-name"


def fixed(name: str, hash_: str) -> str:
    return (
        f'derivation {{ name = "{name}"; system = "x86_64-linux"; builder = "/bin/sh"; '
        f'outputHashMode = "flat"; outputHashAlgo = "sha256"; outputHash = "{hash_}"; }}'
    )


FIXED = fixed("fixed", _REAL)
# The same name and another hash: what Nix builds for a FOD whose hash is empty.
RESOLVED = fixed("fixed", "sha256-47DEQpj8HBSa+/TWmW+nGUhGKf1Kq8BQi/ljyeGd3sQ=")
OTHER = fixed("other", _REAL)
ROOT = (
    f'derivation {{ name = "root"; system = "x86_64-linux"; builder = "/bin/sh"; dep = {FIXED}; }}'
)


@pytest.fixture
async def nix(tmp_path: pathlib.Path) -> AsyncIterator[tuple[Any, Any]]:
    """A chroot store, which writes each `.drv` and needs no builder."""
    async with AsyncSession(str(tmp_path)) as session:
        store = session.store()
        yield store, session.eval(store)


async def drv(nix: tuple[Any, Any], expr: str) -> Any:
    _, state = nix
    return await (await state.eval_expr(expr)).drv_path()


def mismatch(drv_path: str | None) -> FodHashMismatch:
    return FodHashMismatch(specified=_FAKE_SRI, got=_REAL, drv_path=drv_path)


@pytest.mark.parametrize(
    "expr", ["1", "{ }", '{ type = "derivation"; }', '{ type = "other"; drvPath = "x"; }']
)
async def test_a_value_that_is_not_a_derivation_has_no_drv_path(
    nix: tuple[Any, Any], expr: str
) -> None:
    _, state = nix
    assert await evaluated_derivation_path(state, await state.eval_expr(expr)) is None


async def test_a_derivation_answers_its_drv_path(nix: tuple[Any, Any]) -> None:
    _, state = nix
    path = await evaluated_derivation_path(state, await state.eval_expr(ROOT))
    assert path == await drv(nix, ROOT)


async def test_a_fixed_input_is_in_the_closure(nix: tuple[Any, Any]) -> None:
    store, _ = nix
    root = await drv(nix, ROOT)
    assert await is_fixed_output_derivation_in_closure(store, root, await drv(nix, FIXED))
    assert not await is_fixed_output_derivation_in_closure(store, root, root)
    assert not await is_fixed_output_derivation_in_closure(store, root, await drv(nix, OTHER))


async def test_a_resolved_copy_matches_by_name(nix: tuple[Any, Any]) -> None:
    store, _ = nix
    root = await drv(nix, ROOT)
    assert await is_fixed_output_derivation_in_closure(store, root, await drv(nix, RESOLVED))


async def test_a_mismatch_belongs_to_the_target_through_its_closure(
    nix: tuple[Any, Any],
) -> None:
    store, state = nix
    root = await state.eval_expr(ROOT)
    inside = await store.print_store_path(await drv(nix, FIXED))
    outside = await store.print_store_path(await drv(nix, OTHER))
    assert await mismatch_is_target_fod(store, state, root, mismatch(inside))
    assert not await mismatch_is_target_fod(store, state, root, mismatch(outside))


async def test_a_mismatch_with_no_derivation_is_taken_on_the_diagnostic(
    nix: tuple[Any, Any],
) -> None:
    store, state = nix
    assert await mismatch_is_target_fod(store, state, await state.eval_expr("1"), mismatch(None))


async def test_a_target_that_is_not_a_derivation_owns_no_mismatch(nix: tuple[Any, Any]) -> None:
    store, state = nix
    outside = await store.print_store_path(await drv(nix, OTHER))
    assert not await mismatch_is_target_fod(
        store, state, await state.eval_expr("{ }"), mismatch(outside)
    )


async def test_a_fixed_target_owns_its_resolved_copy(nix: tuple[Any, Any]) -> None:
    store, state = nix
    outside = await store.print_store_path(await drv(nix, OTHER))
    assert await mismatch_is_target_fod(
        store, state, await state.eval_expr(FIXED), mismatch(outside)
    )
