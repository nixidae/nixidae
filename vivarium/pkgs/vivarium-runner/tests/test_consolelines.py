"""`ConsoleLines`: a guest's console lines, with no guest."""

from __future__ import annotations

import re
import socket
from collections.abc import Awaitable, Callable

import anyio
import pytest
from anyio.abc import UNIXSocketStream
from anyio.lowlevel import checkpoint
from vivarium_runner.consolelines import LINE_LIMIT, ConsoleLines, GuestExited

READY = re.compile("ready")


def run[T](main: Callable[[], Awaitable[T]]) -> T:
    return anyio.run(main)


async def pumped(console: ConsoleLines, *chunks: bytes) -> None:
    """Pump *chunks* through *console* to the end of the stream."""
    guest, host = socket.socketpair()
    stream = await UNIXSocketStream.from_socket(host)
    async with anyio.create_task_group() as tasks:
        console.start(tasks, stream, "console")
        with guest:
            for chunk in chunks:
                guest.sendall(chunk)
        await console.drained()
    await stream.aclose()


def test_a_line_already_read_matches() -> None:
    said: list[str] = []
    console = ConsoleLines(said.append)

    async def main() -> str:
        await pumped(console, b"\x1b[1mbooting\x1b[0m\r\n", b"\n", b"ready\n")
        return await console.wait_for(READY, anyio.sleep_forever)

    assert run(main) == "ready"
    assert said == ["booting", "ready"]
    assert console.tail() == ["booting", "ready"]


def test_every_waiter_gets_a_later_line() -> None:
    console = ConsoleLines()

    async def main() -> list[str]:
        guest, host = socket.socketpair()
        stream = await UNIXSocketStream.from_socket(host)
        matched: list[str] = []
        async with anyio.create_task_group() as tasks:
            console.start(tasks, stream, "console")

            async def wait() -> None:
                matched.append(await console.wait_for(READY, anyio.sleep_forever))

            tasks.start_soon(wait)
            tasks.start_soon(wait)
            await anyio.wait_all_tasks_blocked()
            with guest:
                guest.sendall(b"booting\nready\n")
        await stream.aclose()
        return matched

    assert run(main) == ["ready", "ready"]


def test_a_line_written_just_before_the_exit_still_matches() -> None:
    console = ConsoleLines()

    async def main() -> str:
        guest, host = socket.socketpair()
        stream = await UNIXSocketStream.from_socket(host)
        async with anyio.create_task_group() as tasks:
            # Gone before the pump has started: the line is still in the socket.
            guest.sendall(b"ready\n")
            guest.close()
            console.start(tasks, stream, "console")
            line = await console.wait_for(READY, checkpoint)
        await stream.aclose()
        return line

    assert run(main) == "ready"


def test_a_wait_goes_on_across_a_reset() -> None:
    console = ConsoleLines()

    async def main() -> str:
        await pumped(console, b"booting\n")
        await console.stop()
        matched: list[str] = []
        async with anyio.create_task_group() as tasks:

            async def wait() -> None:
                matched.append(await console.wait_for(READY, anyio.sleep_forever))

            tasks.start_soon(wait)
            await anyio.wait_all_tasks_blocked()
            console.reset()
            await pumped(console, b"ready\n")
        return matched[0]

    assert run(main) == "ready"


def test_an_overlong_line_is_dropped_and_the_next_is_read() -> None:
    console = ConsoleLines()
    run(lambda: pumped(console, b"x" * (LINE_LIMIT + 10) + b"\n", b"ready\n"))
    assert console.tail()[-1] == "ready"
    assert all(len(line) <= LINE_LIMIT for line in console.tail())


def test_an_unterminated_last_line_is_kept() -> None:
    console = ConsoleLines()
    run(lambda: pumped(console, b"booting\nKernel panic"))
    assert console.tail() == ["booting", "Kernel panic"]


def test_a_guest_gone_ends_the_wait() -> None:
    console = ConsoleLines()

    async def main() -> None:
        with anyio.fail_after(5):
            await console.wait_for(READY, checkpoint)

    with pytest.raises(GuestExited):
        run(main)


def test_stop_keeps_the_lines_and_reset_drops_them() -> None:
    console = ConsoleLines()

    async def main() -> None:
        await pumped(console, b"ready\n")
        await console.stop()
        assert console.tail() == ["ready"]
        assert await console.wait_for(READY, checkpoint) == "ready"
        console.reset()
        assert console.tail() == []
        with pytest.raises(GuestExited), anyio.fail_after(5):
            await console.wait_for(READY, checkpoint)

    run(main)


def test_stop_reads_the_last_line_still_in_the_pipe() -> None:
    """A UML bridge writes the kernel's exit status after the kernel exits."""
    console = ConsoleLines()

    async def main() -> None:
        guest, host = socket.socketpair()
        stream = await UNIXSocketStream.from_socket(host)
        async with anyio.create_task_group() as tasks:
            console.start(tasks, stream, "console")
            with guest:
                guest.sendall(b"the kernel exited (0)\n")
            await console.stop()
        await stream.aclose()

    run(main)
    assert console.tail() == ["the kernel exited (0)"]


def test_stop_ends_when_a_pipe_stays_open() -> None:
    """A process the guest started can hold the pipe open past the exit."""
    console = ConsoleLines()

    async def main() -> None:
        guest, host = socket.socketpair()
        stream = await UNIXSocketStream.from_socket(host)
        with guest:
            async with anyio.create_task_group() as tasks:
                console.start(tasks, stream, "console")
                with anyio.fail_after(5):
                    await console.stop()
        await stream.aclose()

    run(main)
