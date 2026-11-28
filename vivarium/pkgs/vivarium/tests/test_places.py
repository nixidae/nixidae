from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from vivarium import environments, places
from vivarium.control import RUN_FILE, SOCKET, Origin, RunFile

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("name", ["k8s-lb", "a", "x.y_z-1", "0" * 64])
def test_a_name_is_taken(name: str) -> None:
    assert places.check_name(name) == name


@pytest.mark.parametrize("name", ["", "../ca", "ca/x", "K8s", "-x", ".x", "0" * 65, "a b"])
def test_a_name_that_could_leave_envs_is_refused(name: str) -> None:
    with pytest.raises(places.PlaceError):
        places.environment(name)


def test_an_environment_lives_under_state(private_places: Path) -> None:
    out = places.environment("k8s")
    assert out == private_places / "state" / places.ENVIRONMENTS / "k8s"
    assert places.environment_name(out) == "k8s"
    assert places.environment_name(private_places / "elsewhere") is None


def test_a_directory_others_may_enter_is_refused(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    with pytest.raises(places.PlaceError):
        places.private(shared)


def test_prepare_twice_keeps_one_entry(tmp_path: Path) -> None:
    first = places.prepare(tmp_path / "out")
    assert places.prepare(tmp_path / "out") == first
    assert (first / places.OUT_LINK).readlink() == (tmp_path / "out").resolve()
    assert places.socket(tmp_path / "out", SOCKET) == first / SOCKET


def test_remove_refuses_a_directory_outside_envs(tmp_path: Path) -> None:
    keep = tmp_path / "keep"
    keep.mkdir()
    with pytest.raises(environments.Refused):
        environments.remove(keep)
    assert keep.exists()


def test_remove_deletes_an_environment() -> None:
    out = places.environment("gone")
    (out / "access").mkdir(parents=True)
    environments.remove(out)
    assert not out.exists()


@pytest.mark.anyio
async def test_a_run_still_evaluating_counts_as_live() -> None:
    out = places.environment("starting")
    out.mkdir(parents=True)
    about = RunFile(pid=os.getpid(), out=out, vivarium=out / "vivarium", socket=None, commands=())
    (out / RUN_FILE).write_text(about.to_json())
    [one] = await environments.every()
    assert (one.name, one.up) == ("starting", True)


@pytest.mark.anyio
async def test_a_dead_run_is_down() -> None:
    out = places.environment("ended")
    out.mkdir(parents=True)
    about = RunFile(pid=2**22 + 1, out=out, vivarium=out / "vivarium", socket=None, commands=())
    (out / RUN_FILE).write_text(about.to_json())
    [one] = await environments.every()
    assert (one.name, one.up) == ("ended", False)


def test_a_missing_environment_is_named_as_missing() -> None:
    assert environments.why_not_up(places.environment("nosuch")) == (
        "no environment is named nosuch; `vivarium ps` lists them"
    )


def test_a_down_environment_says_how_to_start_it_again() -> None:
    out = places.environment("lab")
    out.mkdir(parents=True)
    origin = Origin(cli=out / "vivarium", attr="checks.browser", file=out / "my tree")
    about = RunFile(pid=2**22 + 1, out=out, vivarium=out / "vivarium", socket=None, commands=(), origin=origin)
    (out / RUN_FILE).write_text(about.to_json())
    assert environments.why_not_up(out) == (
        f"the environment lab is down; start it again: vivarium up checks.browser --file '{out}/my tree' --name lab"
    )
    assert environments.why_not_up(out, again=False) == "the environment lab is down"


def test_a_run_of_ones_own_without_an_origin_is_down(tmp_path: Path) -> None:
    assert environments.why_not_up(tmp_path) == f"no run in {tmp_path}"
    about = RunFile(pid=2**22 + 1, out=tmp_path, vivarium=tmp_path / "vivarium", socket=None, commands=())
    (tmp_path / RUN_FILE).write_text(about.to_json())
    assert environments.why_not_up(tmp_path) == f"the run in {tmp_path} is down"
