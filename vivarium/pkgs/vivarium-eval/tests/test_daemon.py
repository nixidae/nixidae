from __future__ import annotations

from pathlib import Path

from vivarium import places
from vivarium_eval import cli, daemon


def test_unit_name_is_the_environments() -> None:
    assert daemon.unit_name(places.environment("lab")) == "vivarium-lab.service"


def test_unit_name_of_an_own_out_is_its_key(tmp_path: Path) -> None:
    assert daemon.unit_name(tmp_path) == f"vivarium-{places.key(tmp_path)}.service"


def test_passed_leaves_out_systemds_own_and_bad_names() -> None:
    environ = {"PATH": "/bin", "NIX_PATH": "x", "INVOCATION_ID": "1", "NOTIFY_SOCKET": "/s", "A-B": "c", "_X": "y"}
    assert daemon.passed(environ) == ["NIX_PATH", "PATH", "_X"]


def test_argv_carries_names_not_values(tmp_path: Path) -> None:
    words = daemon.argv("systemd-run", "vivarium-lab.service", "d", tmp_path, tmp_path / "run.log", ["SECRET"], ["v"])
    assert words[words.index("--working-directory") + 1] == str(tmp_path)
    assert f"StandardOutput=truncate:{tmp_path / 'run.log'}" in words
    assert words[words.index("-E") : words.index("-E") + 2] == ["-E", "SECRET"]
    assert words[-2:] == ["--", "v"]
    assert "OOMPolicy=continue" in words


def test_the_child_never_sees_the_flag() -> None:
    assert cli.child_args(["up", "lan", "--daemonize", "--out", "o", "--", "-k", "x"]) == [
        "run",
        "lan",
        "--out",
        "o",
        "--hold",
        "--",
        "-k",
        "x",
    ]


def test_daemonize_is_ups_alone() -> None:
    assert cli.parse(["up", "lan", "--out", "o", "--daemonize"]).daemonize
    assert not cli.parse(["up", "lan", "--out", "o"]).daemonize
