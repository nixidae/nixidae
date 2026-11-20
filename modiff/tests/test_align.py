"""List alignment on element keys, without an evaluator."""

from __future__ import annotations

from modiff.walk import SELF, Key, align, describe


def scalars(*values: str) -> list[Key]:
    return [{SELF: v} for v in values]


def test_insert_in_the_middle() -> None:
    assert align(scalars("a", "b", "c"), scalars("a", "x", "b", "c")) == [(0, 0), (None, 1), (1, 2), (2, 3)]


def test_removal_and_replacement() -> None:
    assert align(scalars("a", "b", "c"), scalars("a", "y")) == [(0, 0), (1, None), (2, None), (None, 1)]


def test_moved_unique_element_pairs_once() -> None:
    pairs = align(scalars("a", "b", "c"), scalars("c", "a", "b"))
    assert (0, 1) in pairs and (1, 2) in pairs
    assert sorted(i for i, _ in pairs if i is not None) == [0, 1, 2]
    assert sorted(j for _, j in pairs if j is not None) == [0, 1, 2]


def test_unknown_field_matches_anything() -> None:
    """The base forced `ok`; the new side has not: that is no difference."""
    old: list[Key] = [{SELF: "«attrs»", "ok": "true"}] * 3
    new: list[Key] = [{SELF: "«attrs»"}] * 4
    pairs = align(old, new)
    assert [p for p in pairs if None in p] == [(None, 3)]


def test_literal_field_anchors_an_insert() -> None:
    old: list[Key] = [{SELF: "«attrs»", "name": n} for n in ('"p"', '"q"', '"r"')]
    new: list[Key] = [{SELF: "«attrs»", "name": n} for n in ('"p"', '"new"', '"q"', '"r"')]
    assert align(old, new) == [(0, 0), (None, 1), (1, 2), (2, 3)]


def test_a_forced_bool_does_not_stop_an_anchor() -> None:
    """NixOS `assertions`: the base forced each `assertion`, the new side
    has not, and the literal `message` still names the element."""
    old: list[Key] = [{SELF: "«attrs»", "ok": "true", "msg": m} for m in ('"p"', '"q"')] + [{SELF: "«attrs»"}] * 2
    new: list[Key] = [{SELF: "«attrs»", "msg": m} for m in ('"new"', '"p"', '"q"')] + [{SELF: "«attrs»"}] * 2
    assert [p for p in align(old, new) if None in p] == [(None, 0)]


def test_an_unforced_element_pairs_by_position() -> None:
    assert align([{}, {SELF: "b"}], scalars("x", "b")) == [(0, 0), (1, 1)]


def test_large_stretch_pairs_by_index() -> None:
    old: list[Key] = [{SELF: "«attrs»"}] * 300
    new: list[Key] = [{SELF: "«attrs»"}] * 302
    pairs = align(old, new)
    assert pairs[:300] == [(k, k) for k in range(300)]
    assert pairs[300:] == [(None, 300), (None, 301)]


def test_describe() -> None:
    assert describe({SELF: "zero"}) == "zero"
    assert describe({SELF: "«attrs»", "name": '"p"'}) == '{ name = "p"; }'
    assert describe({}) == "«thunk»"
