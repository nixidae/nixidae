"""`ConsoleLines`: a guest's console lines, with no guest."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable

import anyio
import pytest
from vivarium_runner.consolelines import ConsoleLines, GuestExited

READY = re.compile("ready")


def run[T](main: Callable[[], Awaitable[T]]) -> T:
    return anyio.run(main)


def fed(*lines: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for line in lines:
        reader.feed_data(line)
    reader.feed_eof()
    return reader


def test_a_line_already_read_matches() -> None:
    said: list[str] = []
    console = ConsoleLines(said.append)

    async def main() -> str:
        async with anyio.create_task_group() as tasks:
            console.start(tasks, fed(b"\x1b[1mbooting\x1b[0m\n", b"\n", b"ready\n"), "console")
            await console.drained()
        return await console.wait_for(READY, lambda: False)

    assert run(main) == "ready"
    assert said == ["booting", "ready"]
    assert console.tail() == ["booting", "ready"]


def test_a_later_line_matches() -> None:
    console = ConsoleLines()

    async def main() -> str:
        reader = asyncio.StreamReader()
        async with anyio.create_task_group() as tasks:
            console.start(tasks, reader, "console")
            matched: list[str] = []

            async def wait() -> None:
                matched.append(await console.wait_for(READY, lambda: False))

            tasks.start_soon(wait)
            await anyio.wait_all_tasks_blocked()
            reader.feed_data(b"booting\nready\n")
            reader.feed_eof()
        return matched[0]

    assert run(main) == "ready"


def test_an_exit_ends_the_wait() -> None:
    console = ConsoleLines()

    async def main() -> None:
        with anyio.fail_after(5):
            await console.wait_for(READY, lambda: True)

    with pytest.raises(GuestExited):
        run(main)


def test_stop_keeps_the_lines_and_reset_drops_them() -> None:
    console = ConsoleLines()

    async def main() -> None:
        async with anyio.create_task_group() as tasks:
            console.start(tasks, fed(b"ready\n"), "console")
            await console.drained()
            await console.stop()
        assert console.tail() == ["ready"]
        assert await console.wait_for(READY, lambda: True) == "ready"
        console.reset()
        assert console.tail() == []
        # Neither the history nor the stream holds the first boot's line.
        with pytest.raises(GuestExited), anyio.fail_after(5):
            await console.wait_for(READY, lambda: True)

    run(main)
