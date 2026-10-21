"""One log subscription, many readers over it.

The binding REPLACES a thread's subscription, so a second
`subscribe_logs` on one state silences the first reader (nixidae/nixidae#622).
The server and the local session therefore open one subscription per
state, or one on the process sink, and fan it out here.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import anyio

# What a reader gets when the caller names neither: the binding's own
# defaults (`decl/eval.py`, `EvalState.subscribe_logs`).
LOG_CAPACITY = 1024
LOG_LEVEL = 3


def widest(readers: Iterable[Share]) -> int:
    """The level the shared subscription opens at.

    The WIDEST any reader asked for, and not a constant 7: a
    subscription raises the level `RemoteStore::setOptions` sends to
    the daemon, so subscribing at 7 asks every daemon connection to
    narrate at vomit whether or not anyone wants it (nixidae/nixidae#639). A
    reader that wants more REOPENS the subscription at its level
    rather than being refused."""
    return max((r.level for r in readers), default=LOG_LEVEL)


class Share:
    """One reader's buffer, under the shared queue's bound.

    The DROP POLICY is the C++ queue's, restated because a reader is a
    second bound under the first. A full reader refuses a "msg" and a
    "result" and nothing else, for the reason `LogQueue` gives: a
    dropped stop leaks a node in the reader's activity tree that
    nothing later closes.

    The test names what to DROP, and that is what made `"finalized"`
    safe to add without touching this class. Had it listed what to
    KEEP, a new control event would be lost here in silence.

    `level` filters a "msg" only, for the same reason.
    """

    def __init__(self, capacity: int, level: int) -> None:
        # PUBLIC, because the fan-out opens at the widest of each.
        self.capacity = capacity
        self.level = level
        self._records: list[Any] = []
        self.own_dropped = 0

    def offer(self, record: Any) -> None:
        """One record from the shared drain. Never awaits."""
        action = record.action()
        if action == "msg" and record.level() > self.level:
            return
        if action in ("msg", "result") and len(self._records) >= self.capacity:
            self.own_dropped += 1
            return
        self._records.append(record)

    def has_records(self) -> bool:
        return bool(self._records)

    def take(self) -> list[Any]:
        """Everything this reader holds, and it holds nothing after."""
        out, self._records = self._records, []
        return out


class Fanout:
    """One subscription, opened for the first reader and dropped with
    the last.

    PULL, not push: a reader's `drain` empties the shared queue and
    offers every record to every reader. So no task drains in the
    background. A local session needs no task group for it, and the
    server keeps no loop per subscription. In Python, not in C++: a
    list of queues in `LogTap` would be a mapping no declaration can
    say.

    A reader that wants to WAIT calls `next`. The queue writes a byte
    to a pipe when it stops being empty, and one waiter watches that
    fd, because asyncio lets only one task wait on an fd. Every other
    waiter waits on `_woken`, which a pull that offered records, a
    change of readers, and the watcher's exit all set.

    The shared queue opens at the widest level and the largest
    capacity any reader asked for. A wider reader REOPENS it, after a
    pull hands over what the old queue holds. So the bound a reader
    meets is its own `capacity`, not a smaller shared one.

    The LOCK covers the two transitions that await: no reader to one,
    and one reader to none. `unsubscribe` clears the slot whatever is
    in it, so a teardown that raced the next setup could close the
    queue a newer reader had just installed, and leave that reader
    connected and silent.

    The owner keeps one per state and never removes it on empty. A
    `join` that already holds a removed fan-out would open a
    subscription nobody can find, and the next one would open a
    second, which the binding answers by replacing the first.
    """

    def __init__(self, open_sub: Callable[[int, int], Awaitable[Any]],
                 drop_sub: Callable[[], Awaitable[None]]) -> None:
        self._open = open_sub
        self._drop = drop_sub
        self._lock = anyio.Lock()
        self._sub: Any = None
        self._capacity = 0
        self._level = 0
        self._readers: set[Reader] = set()
        self.dropped = 0
        self._fd = -1
        self._woken = anyio.Event()
        self._watching = False

    def _wake(self) -> None:
        self._woken.set()
        self._woken = anyio.Event()

    async def join(self, capacity: int, level: int) -> Reader:
        """A reader, and the subscription behind it if it is the first."""
        reader = Reader(self, capacity, level)
        async with self._lock:
            if self._sub is not None and (
                    level > self._level or capacity > self._capacity):
                await self._pull()
                await self._close()
            if self._sub is None:
                self._level = max(level, widest(self._readers))
                self._capacity = max(
                    [capacity, *(r.capacity for r in self._readers)])
                self._sub = await self._open(self._capacity, self._level)
                self._fd = await self._sub.fileno()
                self.dropped = 0
                # The subscribe is a call, and its own "finalized"
                # lands in the queue it just installed. No reader asked
                # for that call, so it goes before any reader joins.
                await self._sub.drain()
                # A waiter may watch the fd of the queue just closed.
                self._wake()
            self._readers.add(reader)
        return reader

    async def pull(self) -> None:
        async with self._lock:
            await self._pull()

    async def _pull(self) -> None:
        """LOCK HELD. Offer what the shared queue holds to every reader."""
        if self._sub is None:
            return
        records = await self._sub.drain()
        self.dropped = await self._sub.dropped()
        for record in records:
            for reader in self._readers:
                reader.offer(record)
        if records:
            self._wake()

    async def wait(self, reader: Reader) -> None:
        """Until `reader`'s share may have grown. It may not have: the
        caller pulls and asks again."""
        woken = self._woken
        if reader.has_records() or reader not in self._readers:
            return
        # Held for the wait, so the queue and its fd stay open.
        sub, fd = self._sub, self._fd
        if self._watching or sub is None:
            await woken.wait()
            return
        self._watching = True
        try:
            async with anyio.create_task_group() as tg:
                async def readable() -> None:
                    await anyio.wait_readable(fd)
                    tg.cancel_scope.cancel()

                tg.start_soon(readable)
                await woken.wait()
                tg.cancel_scope.cancel()
        finally:
            self._watching = False
            self._wake()

    async def leave(self, reader: Reader) -> None:
        """Drop a reader, and the subscription with the last one.

        The DISCARD comes before the lock, so a cancel while this
        waits leaves a consistent state: the subscription stays, and
        the next `join` reuses it. Discarded after, a cancelled
        `leave` would keep a reader nobody drains. Not observed;
        nixidae/nixidae#569 tried two perturbations and saw no cancel here."""
        self._readers.discard(reader)
        self._wake()
        async with self._lock:
            if self._readers or self._sub is None:
                return
            await self._close()

    async def _close(self) -> None:
        """LOCK HELD. Drop the shared subscription. A failure goes
        nowhere: the stream it belonged to is over either way."""
        sub, self._sub = self._sub, None
        with contextlib.suppress(Exception):
            await sub.close()
        with contextlib.suppress(Exception):
            await self._drop()


class Reader(Share):
    """One reader's share of a `Fanout`.

    Duck-typed as an `AsyncLogStream` on purpose: a remote
    `subscribe_logs` answers one as a `LogStream` handle, and the
    client's `drain`, `dropped` and `close` calls reach it unchanged.
    """

    def __init__(self, fan: Fanout, capacity: int, level: int) -> None:
        super().__init__(capacity, level)
        self._fan = fan

    async def drain(self) -> list[Any]:
        await self._fan.pull()
        return self.take()

    async def next(self) -> list[Any]:
        """At least one record, after waiting for it. Empty once this
        reader has left."""
        while True:
            records = await self.drain()
            if records or self not in self._fan._readers:
                return records
            await self._fan.wait(self)

    async def fileno(self) -> int:
        """The queue's fd is the fan-out's to watch. A reader waits
        with `next`."""
        raise TypeError("a fan-out reader has no fd; call next()")

    async def dropped(self) -> int:
        """This reader's drops plus the shared queue's. Both are
        cumulative, so the sum is too."""
        return self.own_dropped + self._fan.dropped

    async def close(self) -> None:
        """Leave the fan-out. A reader already gone is a no-op, so the
        reaper and an explicit close can both run."""
        await self._fan.leave(self)

    async def aclose(self) -> None:
        await self.close()
