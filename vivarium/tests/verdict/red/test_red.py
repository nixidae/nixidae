"""A pytest run that fails on purpose, for `checks.verdict`."""

from __future__ import annotations


def test_red() -> None:
    assert 1 + 1 == 3, "fails on purpose"
