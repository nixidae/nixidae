"""The command line hands on absolute paths only. nixidae/nixidae#17."""

from __future__ import annotations

from pathlib import Path

import pytest
from vivarium.cli import parse


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--spec", "spec.json", "--out", "./out", "--kernel", "linux"],
        ["ctl", "--out", "out", "state"],
        ["phases", "--spec", "spec.json"],
    ],
)
def test_a_relative_path_becomes_absolute(argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A guest mounts `<out>/artifacts/<machine>` from another working directory."""
    monkeypatch.chdir(tmp_path)
    args = parse(argv)
    for name in ("spec", "out", "kernel"):
        value = getattr(args, name, None)
        if value is not None:
            assert value.is_absolute(), (name, value)
            assert value.is_relative_to(tmp_path), (name, value)
