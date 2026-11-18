"""The command line hands on absolute paths only. nixidae/nixidae#17."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from vivarium import cli
from vivarium.cli import parse
from vivarium.control import Op, Reply
from vivarium.exits import Exit
from vivarium.session import PerGuest
from vivarium_runner.machine_spec import Cpus

if TYPE_CHECKING:
    from collections.abc import Callable


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
    kernels = cli.kernels_of(args) if argv[0] == "run" else None
    for name, value in (
        ("spec", getattr(args, "spec", None)),
        ("out", getattr(args, "out", None)),
        ("kernel", kernels.every if kernels is not None else None),
    ):
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

    async def request(
        socket: Path, op: Op, arg: str = "", reload: tuple[Path, ...] = (), chunk: Callable[[str], None] | None = None
    ) -> Reply:
        requests.append((op, arg))
        return Reply(ok=True)

    monkeypatch.setattr(cli, "request", request)
    assert await cli.ctl(parse(["ctl", "--out", "out", *argv])) == 0
    assert requests == [(Op(argv[0]), sent.format(cwd=tmp_path))]


@pytest.mark.anyio
async def test_ctl_sends_each_reload_directory_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    sent: list[tuple[Path, ...]] = []

    async def request(
        socket: Path, op: Op, arg: str = "", reload: tuple[Path, ...] = (), chunk: Callable[[str], None] | None = None
    ) -> Reply:
        sent.append(reload)
        return Reply(ok=True)

    monkeypatch.setattr(cli, "request", request)
    argv = ["ctl", "--out", "out", "inject", "check.py", "--reload", "lib", "--reload", "more"]
    assert await cli.ctl(parse(argv)) == 0
    assert sent == [(tmp_path / "lib", tmp_path / "more")]


@pytest.mark.parametrize(
    ("flags", "cpus"),
    [
        ([], None),
        (["--cpus", "4"], PerGuest(every=4)),
        (["--cpu-scale", "0.5"], PerGuest(every={"scale": 0.5})),
        (
            ["--cpus", "2", "--cpus", "db=8", "--cpu-scale", "web=0.5"],
            PerGuest(every=2, guests={"db": 8, "web": {"scale": 0.5}}),
        ),
    ],
)
def test_cpus_of_a_run(flags: list[str], cpus: PerGuest[Cpus] | None) -> None:
    assert cli.cpus_of(parse(["run", "--spec", "s.json", "--out", "o", *flags])) == cpus


@pytest.mark.parametrize(
    ("flags", "kernels"),
    [
        ([], None),
        (["--kernel", "/k/linux"], PerGuest(every=Path("/k/linux"))),
        (["--kernel", "/k/linux", "--kernel", "db=/d/bzImage"], PerGuest(Path("/k/linux"), {"db": Path("/d/bzImage")})),
        (["--kernel", "/k/a=b/linux"], PerGuest(every=Path("/k/a=b/linux"))),
    ],
)
def test_kernels_of_a_run(flags: list[str], kernels: PerGuest[Path] | None) -> None:
    assert cli.kernels_of(parse(["run", "--spec", "s.json", "--out", "o", *flags])) == kernels


def test_a_relative_kernel_is_made_absolute() -> None:
    got = cli.kernels_of(parse(["run", "--spec", "s.json", "--out", "o", "--kernel", "db=linux"]))
    assert got == PerGuest(guests={"db": Path("linux").absolute()})


def test_a_guest_s_kernel_is_given_once() -> None:
    with pytest.raises(SystemExit, match="give db's kernel once"):
        cli.kernels_of(parse(["run", "--spec", "s.json", "--out", "o", "--kernel", "db=/a", "--kernel", "db=/b"]))


@pytest.mark.parametrize(
    "flags",
    [
        ["--cpus", "0"],
        ["--cpu-scale", "-1"],
        ["--cpus", "x"],
        ["--cpus", "2", "--cpu-scale", "1"],
        ["--cpus", "db=2", "--cpu-scale", "db=1"],
    ],
)
def test_cpus_refused(flags: list[str]) -> None:
    with pytest.raises(SystemExit):
        cli.cpus_of(parse(["run", "--spec", "s.json", "--out", "o", *flags]))


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("answer", "status"),
    [
        (Reply(ok=True), Exit.PASSED),
        (Reply(ok=False, error="ZeroDivisionError"), Exit.FAILED),
        (Reply.refuse("exec only while paused"), Exit.REFUSED),
        (OSError("no such file"), Exit.NO_RUN),
    ],
)
async def test_ctl_tells_a_refusal_from_a_failure_and_no_run(
    answer: Reply | OSError, status: Exit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each its own status: a script that sees 1 knows the request ran. nixidae/nixidae#1483."""

    async def request(
        socket: Path, op: Op, arg: str = "", reload: tuple[Path, ...] = (), chunk: Callable[[str], None] | None = None
    ) -> Reply:
        if isinstance(answer, OSError):
            raise answer
        return answer

    monkeypatch.setattr(cli, "request", request)
    for json in ([], ["--json"]):
        assert await cli.ctl(parse(["ctl", "--out", "out", *json, "exec", "1"])) is status
