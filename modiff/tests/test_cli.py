from __future__ import annotations

from pathlib import Path

import pytest

from modiff.cli import parse_args


def test_a_missing_nixos_file_stops_before_the_app(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        parse_args(["--nixos", str(tmp_path / "missing.nix")])
    assert stopped.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1].endswith("missing.nix: no such file")


def test_an_existing_nixos_file_parses(tmp_path: Path) -> None:
    config = tmp_path / "configuration.nix"
    config.write_text("{ }")
    assert parse_args(["--nixos", str(config)]).nixos == config
