"""A run's channel events, served on its control socket: `ctl watch`.

The events `channel_event` picks out -- progress, a pause, a failed phase,
the verdict -- kept in memory and sent to every watcher: the ones so far,
then `LIVE`, then each as it happens. `vivarium monitor` and `vivarium-mcp`
both read this, so every run with a control socket has one live stream,
and nothing tails `events.jsonl` to find out what happened.

A pytest phase emits from a worker thread, and a memory stream belongs to
the event loop's thread, so an event from any other thread crosses back
through `anyio.from_thread`.
"""

from __future__ import annotations

import contextlib
import math
import threading
from typing import TYPE_CHECKING

import anyio

from .events import Kind
from .monitor import LIVE, channel_event

if TYPE_CHECKING:
    from collections.abc import Iterator

    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

    from .events import Event

Item = dict[str, str]
"""One channel event as the monitor reads it: its meta keys and `text`."""

KINDS = frozenset({Kind.NOTE, Kind.PHASE_STARTED, Kind.PHASE_FINISHED, Kind.RUN_FINISHED})
"""The kinds `channel_event` can pick; the rest cost nothing here."""


class Feed:
    """A sink that keeps a run's channel events for its watchers."""

    def __init__(self, run: str) -> None:
        self.run = run
        self._seen: list[Item] = []
        self._watchers: set[MemoryObjectSendStream[Item]] = set()
        self._closed = False
        self._active = 0
        self._idle = anyio.Event()
        self._idle.set()
        self._thread = threading.get_ident()

    def emit(self, event: Event) -> None:
        if event.kind not in KINDS:
            return
        pushed = channel_event(event.as_dict(), self.run)
        if pushed is None:
            return
        item = {**pushed[1], "text": pushed[0]}
        if threading.get_ident() == self._thread:
            self._push(item)
        else:
            anyio.from_thread.run_sync(self._push, item)

    def _push(self, item: Item) -> None:
        self._seen.append(item)
        for watcher in list(self._watchers):
            try:
                watcher.send_nowait(item)
            except anyio.BrokenResourceError:
                # The watcher hung up; `watch` forgets it on its way out.
                self._watchers.discard(watcher)

    def close(self) -> None:
        """No more events: each watcher's stream ends after what it holds."""
        self._closed = True
        for watcher in self._watchers:
            watcher.close()
        self._watchers.clear()

    async def drained(self) -> None:
        """Until every watcher has sent what it holds and hung up."""
        await self._idle.wait()

    @contextlib.contextmanager
    def watch(self) -> Iterator[MemoryObjectReceiveStream[Item]]:
        """The events so far, `LIVE`, then each new one until `close`."""
        send, receive = anyio.create_memory_object_stream[Item](math.inf)
        for item in self._seen:
            send.send_nowait(item)
        send.send_nowait(LIVE)
        if self._closed:
            send.close()
        else:
            self._watchers.add(send)
        if self._active == 0:
            self._idle = anyio.Event()
        self._active += 1
        try:
            with receive:
                yield receive
        finally:
            self._watchers.discard(send)
            send.close()
            self._active -= 1
            if self._active == 0:
                self._idle.set()
