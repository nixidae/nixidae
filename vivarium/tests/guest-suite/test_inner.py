from __future__ import annotations


def test_ok() -> None:
    assert sum([1]) == 1


def test_bad() -> None:
    assert sum([1]) == 2
