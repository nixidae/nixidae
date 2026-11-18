"""A held task: stopped from another task, before or after it starts, and its error reaches the owner."""

from __future__ import annotations

import anyio
import pytest
from vivarium_runner.held import hold


def test_a_stop_before_the_task_starts_keeps_its_body_from_running() -> None:
    ran: list[str] = []

    async def body() -> None:
        ran.append("body")

    async def main() -> None:
        async with anyio.create_task_group() as group:
            await hold(group, body).stop()

    anyio.run(main)
    assert ran == []


def test_stop_cancels_a_running_task_and_waits_for_its_cleanup() -> None:
    events: list[str] = []

    async def body() -> None:
        try:
            events.append("started")
            await anyio.sleep_forever()
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.lowlevel.checkpoint()
            events.append("cleaned up")

    async def main() -> None:
        async with anyio.create_task_group() as group:
            held = hold(group, body)
            await anyio.wait_all_tasks_blocked()
            await held.stop()
            events.append("stopped")

    anyio.run(main)
    assert events == ["started", "cleaned up", "stopped"]


def test_an_error_in_a_held_task_cancels_its_owner() -> None:
    class Boom(Exception):
        pass

    async def body() -> None:
        raise Boom

    async def main() -> None:
        async with anyio.create_task_group() as group:
            hold(group, body)
            await anyio.sleep_forever()

    with pytest.raises(ExceptionGroup) as caught:
        anyio.run(main)
    assert caught.group_contains(Boom)
