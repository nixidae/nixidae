"""A guest's console as lines: kept for a tail, waited on for a pattern.

The lines come from the guest process's stdout, and for a guest with no
control channel from its serial line too. `control.ConsoleControl` is a
different thing: it logs in on a serial console and runs commands there.
"""

from __future__ import annotations

import collections
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import anyio
from anyio.abc import ByteReceiveStream, TaskGroup, UNIXSocketStream
from anyio.streams.buffered import BufferedByteReceiveStream

from .held import Held, hold

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

HISTORY = 2000
"""Console lines kept for `ConsoleLines.tail`."""

LINE_LIMIT = 65536
"""Bytes in one console line; a longer one is dropped."""

EXIT_DRAIN = 0.5
"""Seconds a wait still reads the console once the guest is gone.

What the guest wrote just before it went can still be in the pipe. A
bound and not a wait for the end of the pipe, because a process the
guest started can hold the pipe open past the exit.
"""


@dataclass(eq=False, slots=True)
class _Waiter:
    pattern: re.Pattern[str]
    matched: anyio.Event = field(default_factory=anyio.Event)
    line: str | None = None


class GuestExited(Exception):
    """The guest was gone before its console matched."""


class ConsoleLines:
    """One guest's console lines, across its boots.

    A boot starts the pumps; `stop` ends them and keeps the lines, which
    an error about a guest that is gone still reads. `reset` drops them
    for the next boot.
    """

    def __init__(self, on_line: Callable[[str], None] | None = None) -> None:
        self.on_line = on_line
        self.history: collections.deque[str] = collections.deque(maxlen=HISTORY)
        self._waiters: set[_Waiter] = set()
        self._stdout: Held | None = None
        self._serial: Held | None = None
        self._serial_stream: UNIXSocketStream | None = None

    def start(self, tasks: TaskGroup, stdout: ByteReceiveStream, name: str) -> None:
        """Pump the guest process's *stdout*, in *tasks*."""
        self._stdout = hold(tasks, self._pump, stdout, name=name)

    async def read_serial(self, tasks: TaskGroup, sock: socket.socket, name: str) -> None:
        """Pump the serial line on *sock* as well, in *tasks*.

        It is read to the end, or the guest stalls once the socket buffer
        is full.
        """
        self._serial_stream = await UNIXSocketStream.from_socket(sock)
        self._serial = hold(tasks, self._pump, self._serial_stream, name=name)

    async def drained(self) -> None:
        """Until the stdout pump has read to the end."""
        if self._stdout is not None:
            await self._stdout.wait()

    async def stop(self) -> None:
        """End the pumps of this boot, once the guest process is gone. The lines stay.

        Stdout is read to its end first, for `EXIT_DRAIN` at most: the last
        line can still be in the pipe, and a UML guest's bridge writes its
        exit status there after the kernel exits.
        """
        with anyio.move_on_after(EXIT_DRAIN):
            await self.drained()
        for task in (self._stdout, self._serial):
            if task is not None:
                await task.stop()
        self._stdout = self._serial = None
        if self._serial_stream is not None:
            await self._serial_stream.aclose()
            self._serial_stream = None

    def reset(self) -> None:
        """Drop every line, so a second boot does not match the first boot's.

        After `stop`: a pump still running can put a line of the old boot
        back after the clear. A wait goes on, on the next boot's lines.
        """
        self.history.clear()

    def tail(self, lines: int = 15) -> list[str]:
        """The last *lines* lines, oldest first."""
        return list(self.history)[-lines:]

    async def wait_for(self, pattern: re.Pattern[str], gone: Callable[[], Awaitable[object]]) -> str:
        """The first line, past or future, that *pattern* matches.

        *gone* returns when the guest is gone. The wait then reads for
        `EXIT_DRAIN` more seconds, and raises `GuestExited`.
        """
        for line in self.history:
            if pattern.search(line):
                return line
        waiter = _Waiter(pattern)
        self._waiters.add(waiter)
        try:
            async with anyio.create_task_group() as tasks:

                async def on_gone() -> None:
                    await gone()
                    with anyio.move_on_after(EXIT_DRAIN):
                        for task in (self._stdout, self._serial):
                            if task is not None:
                                await task.wait()
                    tasks.cancel_scope.cancel()

                tasks.start_soon(on_gone)
                await waiter.matched.wait()
                tasks.cancel_scope.cancel()
        finally:
            self._waiters.discard(waiter)
        if waiter.line is None:
            raise GuestExited
        return waiter.line

    async def _pump(self, stream: ByteReceiveStream) -> None:
        buffered = BufferedByteReceiveStream(stream)
        while True:
            try:
                raw = await buffered.receive_until(b"\n", LINE_LIMIT)
            except anyio.DelimiterNotFound:
                # The buffer keeps what it read: drop it, rather than die.
                await buffered.receive(LINE_LIMIT)
                continue
            except anyio.IncompleteRead:
                # The end, with the last line unterminated.
                self._take(buffered.buffer)
                return
            except (OSError, anyio.BrokenResourceError) as error:
                # The guest's end went away mid-read: the console ends, and
                # the guest's exit says the rest. Raised, it ends the run.
                self._say(f"the console ended: {error}")
                return
            self._take(raw)

    def _take(self, raw: bytes) -> None:
        line = _ANSI.sub("", raw.decode(errors="replace").rstrip())
        if not line:
            return
        self._say(line)
        self.history.append(line)
        for waiter in [waiter for waiter in self._waiters if waiter.pattern.search(line)]:
            waiter.line = line
            waiter.matched.set()
            self._waiters.discard(waiter)

    def _say(self, line: str) -> None:
        if self.on_line is not None:
            self.on_line(line)
