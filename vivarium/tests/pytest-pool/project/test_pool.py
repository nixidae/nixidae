"""The run's pool, as tests see it: fresh guests for clean scopes, one
shared guest for dirty ones, a pytest worker that dirty scopes inside
that guest share, and a test that runs inside a pooled guest.

pytest runs the tests in file order; each records the guest it got.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from vivarium.remote import RemoteMachine

GOT: dict[str, str] = {}
"""Each test's guest name."""

MARK = "/root/mark"


async def marked(vm: RemoteMachine) -> bool:
    return (await vm.execute(f"test -e {MARK}")).returncode == 0


@pytest.mark.vivarium("boxes", template="box", scope="function")
async def test_clean_first(vm: RemoteMachine) -> None:
    GOT["clean_first"] = vm.name
    await vm.succeed(f"touch {MARK}")


@pytest.mark.vivarium("boxes", template="box", scope="function")
async def test_clean_second_is_fresh(vm: RemoteMachine) -> None:
    GOT["clean_second"] = vm.name
    assert vm.name != GOT["clean_first"]
    assert not await marked(vm)


@pytest.mark.vivarium("boxes", template="box", scope="function", dirty=True)
async def test_dirty_first(vm: RemoteMachine) -> None:
    GOT["dirty_first"] = vm.name
    await vm.succeed(f"touch {MARK}")


@pytest.mark.vivarium("boxes", template="box", scope="function", dirty=True)
async def test_dirty_second_shares_it(vm: RemoteMachine) -> None:
    assert vm.name == GOT["dirty_first"]
    assert await marked(vm)


WORKER: dict[str, int] = {}
"""In the guest's worker: the pid of the process that ran the first dirty test inside."""


@pytest.mark.vivarium("boxes", template="box", scope="function", guest="box", dirty=True)
def test_dirty_inside_first() -> None:
    WORKER["pid"] = os.getpid()


@pytest.mark.vivarium("boxes", template="box", scope="function", guest="box", dirty=True)
def test_dirty_inside_reuses_the_worker() -> None:
    assert WORKER.get("pid") == os.getpid(), "a dirty guest keeps its pytest worker for the next scope"


@pytest.mark.vivarium("boxes", template="box", scope="function", guest="box")
def test_runs_inside_a_pooled_guest() -> None:
    assert Path("/proc/1/comm").read_text().strip() == "dinit"
