from __future__ import annotations

from pathlib import Path

import huggorm
import pytest

from modiff import cli
from modiff.cli import parse_args


def test_main_reads_nix_conf_before_the_engine_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(huggorm, "load_config", lambda: calls.append("load_config"))
    monkeypatch.setattr(huggorm, "init_plugins", lambda: calls.append("init_plugins"))
    monkeypatch.setattr(cli, "EngineThread", lambda source: calls.append("engine"))

    class App:
        def __init__(self, engine: object, edits: object) -> None:
            pass

        def run(self) -> None:
            calls.append("run")

    monkeypatch.setattr(cli, "ModiffApp", App)
    cli.main([])
    assert calls == ["load_config", "init_plugins", "engine", "run"]


def test_a_missing_nixos_file_stops_before_the_app(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        parse_args(["--nixos", str(tmp_path / "missing.nix")])
    assert stopped.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1].endswith("missing.nix: no such file or directory")


def test_an_existing_nixos_file_parses(tmp_path: Path) -> None:
    config = tmp_path / "configuration.nix"
    config.write_text("{ }")
    assert parse_args(["--nixos", str(config)]).nixos == config


def test_a_nixos_directory_parses(tmp_path: Path) -> None:
    (tmp_path / "default.nix").write_text("{ }")
    assert parse_args(["--nixos", str(tmp_path)]).nixos == tmp_path
