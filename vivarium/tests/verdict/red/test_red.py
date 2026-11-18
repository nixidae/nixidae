"""A pytest run that fails on purpose, for `checks.verdict`."""

from __future__ import annotations

import pytest


def test_red() -> None:
    pytest.fail("fails on purpose")
