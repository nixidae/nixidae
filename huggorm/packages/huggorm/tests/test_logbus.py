"""The fan-out's wait, over a subscription that wakes as the C++
`LogQueue` does: one byte on a pipe when the queue stops being
empty."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import anyio
import pytest

from huggorm.logbus import Fanout, Reader

pytestmark = pytest.mark.anyio


@dataclass
class Record:
    said: str

    def action(self) -> str:
        return "msg"

    def level(self) -> int:
        return 0


class Sub:
    """`LogQueue`'s wake, in Python."""

    def __init__(self) -> None:
        self.read_end, self.write_end = os.pipe()
        os.set_blocking(self.read_end, False)
        self.records: list[Record] = []
        self.signalled = False
        self.closed = False

    def push(self, said: str) -> None:
        self.records.append(Record(said))
        if not self.signalled:
            self.signalled = True
            os.write(self.write_end, b"\0")

    async def drain(self) -> list[Record]:
        out, self.records = self.records, []
        if self.signalled:
            os.read(self.read_end, 64)
            self.signalled = False
        return out

    async def dropped(self) -> int:
        return 0

    async def fileno(self) -> int:
        return self.read_end

    async def close(self) -> None:
        self.closed = True


class Opened:
    """Every subscription the fan-out opened, newest last."""

    def __init__(self) -> None:
        self.subs: list[Sub] = []

    async def open(self, capacity: int, level: int) -> Sub:
        self.subs.append(Sub())
        return self.subs[-1]

    async def drop(self) -> None:
        pass


def said(records: list[Any]) -> list[str]:
    return [r.said for r in records]


async def test_one_push_wakes_every_waiting_reader() -> None:
    """Only one task may wait on an fd, so the fan-out wakes the rest."""
    opened = Opened()
    fan = Fanout(opened.open, opened.drop)
    readers = [await fan.join(8, 0) for _ in range(3)]
    got: dict[int, list[str]] = {}

    async def read(i: int, reader: Reader) -> None:
        got[i] = said(await reader.next())

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            for i, reader in enumerate(readers):
                tg.start_soon(read, i, reader)
            await anyio.wait_all_tasks_blocked()
            opened.subs[-1].push("one")
    assert got == {0: ["one"], 1: ["one"], 2: ["one"]}


async def test_a_record_another_reader_pulled_wakes_its_waiter() -> None:
    """A `drain` by one reader offers to all, and a waiter whose share
    filled that way wakes, although the pipe is empty again."""
    opened = Opened()
    fan = Fanout(opened.open, opened.drop)
    waiting, draining = await fan.join(8, 0), await fan.join(8, 0)
    got: list[str] = []

    async def read() -> None:
        got.extend(said(await waiting.next()))

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(read)
            await anyio.wait_all_tasks_blocked()
            opened.subs[-1].records.append(Record("quiet"))
            assert said(await draining.drain()) == ["quiet"]
    assert got == ["quiet"]


async def test_a_cancelled_watcher_hands_the_fd_to_the_next_waiter() -> None:
    """Perturbation: drop the wake on the watcher's way out, and the
    reader left waits on an event nobody sets."""
    opened = Opened()
    fan = Fanout(opened.open, opened.drop)
    first, second = await fan.join(8, 0), await fan.join(8, 0)
    got: list[str] = []

    async def read() -> None:
        got.extend(said(await second.next()))

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            async with anyio.create_task_group() as watcher:
                watcher.start_soon(first.next)
                await anyio.wait_all_tasks_blocked()
                assert fan._watching
                tg.start_soon(read)
                await anyio.wait_all_tasks_blocked()
                watcher.cancel_scope.cancel()
            await anyio.wait_all_tasks_blocked()
            opened.subs[-1].push("handed")
    assert got == ["handed"]


async def test_a_waiter_survives_a_wider_reader_reopening() -> None:
    """The waiter watched the old queue's fd. Perturbation: drop the
    wake in `Fanout.join`, and it never hears the new one."""
    opened = Opened()
    fan = Fanout(opened.open, opened.drop)
    narrow = await fan.join(8, 0)
    got: list[str] = []

    async def read() -> None:
        got.extend(said(await narrow.next()))

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(read)
            await anyio.wait_all_tasks_blocked()
            await fan.join(8, 7)
            assert len(opened.subs) == 2, "the wider reader reopened"
            await anyio.wait_all_tasks_blocked()
            opened.subs[-1].push("after")
    assert got == ["after"]


async def test_closing_a_reader_ends_its_wait() -> None:
    """Perturbation: drop the wake in `Fanout.leave`, and the wait
    never ends."""
    opened = Opened()
    fan = Fanout(opened.open, opened.drop)
    reader = await fan.join(8, 0)
    got: list[list[Any]] = []

    async def read() -> None:
        got.append(await reader.next())

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(read)
            await anyio.wait_all_tasks_blocked()
            await reader.close()
    assert got == [[]]
    assert opened.subs[-1].closed
