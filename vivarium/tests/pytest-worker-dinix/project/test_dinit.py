"""The guest that runs these tests is a dinix one: dinit is its PID 1."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.vivarium("one", guest="vm", scope="session")


def test_runs_under_dinit() -> None:
    assert Path("/proc/1/comm").read_text().strip() == "dinit"
