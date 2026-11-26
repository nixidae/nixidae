"""Async fixtures of a test's own use its guests (nixidae/nixidae#1413).

A fixture of a test marked `vivarium` runs on the plugin's loop, where the
guests' connections are. A task group it holds across `yield` works:
setup and teardown are one task.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

MARK = "/tmp/marked"


@pytest.fixture
async def marked(vm) -> AsyncGenerator[str]:  # noqa: ANN001 -- a RemoteMachine
    async with anyio.create_task_group() as group:
        group.start_soon(anyio.sleep_forever)
        await vm.succeed(f"touch {MARK}")
        yield MARK
        await vm.succeed(f"rm {MARK}")
        group.cancel_scope.cancel()


@pytest.fixture
async def hostname(vm) -> str:  # noqa: ANN001 -- a RemoteMachine
    return (await vm.succeed("hostname")).stdout.strip()


@pytest.mark.vivarium("one", scope="module")
async def test_async_fixtures_use_the_guest(vm, marked: str, hostname: str) -> None:  # noqa: ANN001 -- a RemoteMachine
    assert hostname == "vm"
    await vm.succeed(f"test -e {marked}")


@pytest.mark.vivarium("one", scope="module")
async def test_the_teardown_ran(vm) -> None:  # noqa: ANN001 -- a RemoteMachine
    assert (await vm.execute(f"test -e {MARK}")).returncode != 0


@pytest.mark.vivarium("one", scope="module")
def test_a_sync_test_takes_an_async_fixture(hostname: str) -> None:
    assert hostname == "vm"


@pytest.mark.vivarium("one", scope="module")
class TestMethodFixture:
    factor = 3

    @pytest.fixture
    async def doubled(self) -> int:
        return self.factor * 2

    def test_the_fixture_is_bound_to_the_instance(self, doubled: int) -> None:
        assert doubled == 6
