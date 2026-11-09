"""The command line hands on absolute paths only. nixidae/nixidae#17."""

from __future__ import annotations

from pathlib import Path

import pytest
from vivarium import cli
from vivarium.cli import parse
from vivarium.control import Op, Reply


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


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("argv", "sent"),
    [
        (["inject", "check.py"], "{cwd}/check.py"),
        (["pytest", "tests", "--", "-k", "x"], "{cwd}/tests -k x"),
        (["run", "check"], "check"),
    ],
)
async def test_ctl_sends_a_file_as_an_absolute_path(
    argv: list[str], sent: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run resolves a path against its own directory. nixidae/nixidae#856."""
    monkeypatch.chdir(tmp_path)
    requests: list[tuple[Op, str]] = []

    async def request(socket: Path, op: Op, arg: str = "", reload: tuple[Path, ...] = ()) -> Reply:
        requests.append((op, arg))
        return Reply(ok=True)

    monkeypatch.setattr(cli, "request", request)
    assert await cli.ctl(parse(["ctl", "--out", "out", *argv])) == 0
    assert requests == [(Op(argv[0]), sent.format(cwd=tmp_path))]


@pytest.mark.anyio
async def test_ctl_sends_each_reload_directory_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    sent: list[tuple[Path, ...]] = []

    async def request(socket: Path, op: Op, arg: str = "", reload: tuple[Path, ...] = ()) -> Reply:
        sent.append(reload)
        return Reply(ok=True)

    monkeypatch.setattr(cli, "request", request)
    argv = ["ctl", "--out", "out", "inject", "check.py", "--reload", "lib", "--reload", "more"]
    assert await cli.ctl(parse(argv)) == 0
    assert sent == [(tmp_path / "lib", tmp_path / "more")]
