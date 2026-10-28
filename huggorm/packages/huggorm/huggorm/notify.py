"""
The part that NOTICES, without being asked.

`watch.Watcher` decides what to forget when a path changes, and
`Watcher.rescan()` finds a change by stat-ing every watched file. This
is the other change source: the kernel tells us instead.

It is a thin adapter and that is the whole design. nixidae/nixidae#620 kept
noticing separate from bookkeeping so a third caller of
`Watcher.changed()` would be an addition rather than a rewrite, and
this is that third caller. Nothing here decides what a change MEANS.

## Directories, not files

Watches go on the parent directory of each watched file, never on the
file. Most editors save by writing a temporary file and renaming it
over the original, which replaces the inode - and a watch follows the
inode, so a file watch survives the save while pointing at the file
nobody has any more. The rename is a `MOVED_TO` in the parent, so a
directory watch sees exactly the event a file watch misses.

It costs the events for every other file in that directory. Those are
dropped here rather than passed on: `changed()` on a path no root
depends on already answers nothing, but it takes the watcher's lock
to say so, and a directory holding a checkout raises thousands of
them.

## watchdog, on a thread of its own

`watchdog` is the engine: inotify on Linux, FSEvents on macOS. It
replaced `asyncinotify`, which nixidae/nixidae#620 chose for being
asyncio-native and which is Linux only; Carl asked for macOS, and for
nothing past "this path changed". pynixd made the same move for the
same reason (nixidae/nixidae#719).

The observer runs on its own thread. Its handler hands each event to
the event loop with `anyio.from_thread.run_sync`, into a memory stream
that `next_change()` receives from, so nothing polls. That call blocks
the observer thread until the loop takes the event, so `close()` stops
the observer and never joins it: a join from the loop would wait on a
thread that waits on the loop.

One gap against inotify: a watched directory that is RENAMED away gives
no event, because watchdog emits nothing for a non-recursive watch's
`MOVE_SELF`. A deleted one does. `Watcher.rescan()` finds the rename,
and it is still a complete change source that needs no dependency.

## What it inherits, and what it adds

Every limit in `watch.py` still holds: a `builtins.readFile` is in no
cache and so in no watch, and the files are THIS machine's, so a
remote state's are on the server.

## Waiting adds watches; only `sync()` removes them

`next_change()` adds the directories the watcher depends on each time
it starts to wait, so an evaluation through `Watcher.eval_file` is
watched from the next wait on, with no call in between.

It never removes one. A change forgets its root, so the root's
directory drops out of what the watcher depends on until something
evaluates the root again. Removing the watch in that gap left a
re-evaluated root unwatched for good: three saves, one evaluation
(nixidae/nixidae#878). Keeping it costs only events that `_paths`
drops. An explicit `sync()` prunes.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from anyio import from_thread
from anyio.lowlevel import current_token
from watchdog.events import (
    EVENT_TYPE_CREATED,
    EVENT_TYPE_DELETED,
    EVENT_TYPE_MODIFIED,
    EVENT_TYPE_MOVED,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

if TYPE_CHECKING:
    from anyio.lowlevel import EventLoopToken
    from anyio.streams.memory import MemoryObjectSendStream

    from huggorm_generated import AsyncValue

    from .watch import Watcher

# What counts as "this file is not what it was": a write, a file
# appearing or leaving, and the rename an editor saves with, which a
# file watch cannot see at all.
#
# `closed` (close-after-write) is deliberately absent. It is one event
# per save where a modify is one per write(2) - and a writer using mmap
# never closes anything, so it would also be zero events for a real
# change. Over-reporting costs a call that forgets nothing;
# under-reporting is silent staleness. `opened` and `closed_no_write`
# are reads, and an evaluation reads.
CHANGES = frozenset({EVENT_TYPE_MODIFIED, EVENT_TYPE_CREATED, EVENT_TYPE_DELETED, EVENT_TYPE_MOVED})


class _Forward(FileSystemEventHandler):
    """Runs on the observer's thread, and only hands events over."""

    def __init__(self, send: MemoryObjectSendStream[FileSystemEvent], token: EventLoopToken) -> None:
        self._send = send
        self._token = token

    def on_any_event(self, event: FileSystemEvent) -> None:
        # The loop has ended, or the notifier closed its stream: either
        # way nobody will receive this event, and the thread just ends.
        with contextlib.suppress(anyio.RunFinishedError, anyio.ClosedResourceError, anyio.BrokenResourceError):
            from_thread.run_sync(self._send.send_nowait, event, token=self._token)


class Notifier:
    """A file-change source over one `Watcher`.

    Owns the observer thread, so it is a context manager:

        async with Notifier(watcher) as source:
            await source.eval_file("/path/to/root.nix")
            while True:
                await source.next_change()

    `next_change()` is the whole loop body, and `run()` is that loop.
    The pair exists because a gate needs to advance ONE event and then
    assert - a test driving `run()` could only wait and hope.
    """

    def __init__(self, watcher: Watcher) -> None:
        self._watcher = watcher
        # Unbounded: a bound would drop an event when full, and a
        # dropped event is silent staleness. An event is a few paths.
        self._send, self._receive = anyio.create_memory_object_stream[FileSystemEvent](float("inf"))
        self._observer: Any = None
        self._handler: _Forward | None = None
        # directory -> the watch on it. The key is what `sync` diffs,
        # and the value is what `unschedule` needs.
        self._watches: dict[str, Any] = {}

    # -- lifetime ---------------------------------------------------------
    async def __aenter__(self) -> Notifier:
        await self.sync()
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Stop the observer. The watcher is untouched.

        No join: the module docstring says why."""
        self._watches.clear()
        self._send.close()
        self._receive.close()
        if self._observer is not None:
            self._observer.stop()
            self._observer = None

    # -- the watch set ----------------------------------------------------
    def directories(self) -> list[str]:
        """The directories currently watched."""
        return sorted(self._watches)

    async def sync(self) -> list[str]:
        """Make the watch set match what the watcher depends on.

        Returns the roots it forgot on the way, which is normally
        empty: a directory that has gone away since it was cached is a
        change, and this is where that is noticed.

        Idempotent, and cheap when nothing moved - it diffs two sets of
        directory names and touches the kernel only for the
        difference. The prune is the only removal; waiting only adds.
        """
        wanted = _parents(self._watcher.watching())
        for gone in sorted(set(self._watches) - wanted):
            # Unscheduling a directory the kernel already dropped -
            # deleted, unmounted - raises, and the watch is gone
            # either way. There is nothing to report and nothing to
            # do.
            with contextlib.suppress(OSError, KeyError):
                self._observer.unschedule(self._watches[gone])
            del self._watches[gone]
        return await self._add(wanted)

    def _started(self) -> Any:
        """The observer, started on first use, from inside the loop:
        the handler needs the loop's token to reach it."""
        if self._observer is None:
            self._handler = _Forward(self._send, current_token())
            self._observer = Observer()
            self._observer.start()
        return self._observer

    async def _add(self, wanted: set[str]) -> list[str]:
        """Watch every directory in `wanted` not yet watched."""
        forgotten: list[str] = []
        for new in sorted(wanted - set(self._watches)):
            try:
                # Checked first: a started observer raises for a missing
                # directory, but the check does not depend on that.
                if not os.path.isdir(new):
                    raise FileNotFoundError(new)
                self._watches[new] = self._started().schedule(self._handler, new, recursive=False)
            except OSError:
                # The directory is not there. Every file the watcher
                # holds under it is therefore gone, which is a change
                # and not a reason to skip: skipping would leave those
                # roots cached against files that no longer exist,
                # with nothing said.
                for orphan in self._watcher.watching():
                    if os.path.dirname(orphan) == new:
                        forgotten += await self._watcher.changed(orphan)
        return sorted(set(forgotten))

    async def eval_file(self, path: str) -> AsyncValue:
        """Evaluate a root through the watcher, then watch its files.

        The convenience, not a third owner of `eval_file`: the answer
        is the watcher's. It watches now rather than at the next
        wait."""
        value = await self._watcher.eval_file(path)
        await self._add(_parents(self._watcher.watching()))
        return value

    # -- the loop ---------------------------------------------------------
    async def next_change(self) -> list[str]:
        """Wait for one event that matters, act on it, and answer.

        Returns the roots forgotten. It waits as long as it has to:
        events for files no root depends on are read and dropped here
        rather than passed on, because a watched directory can hold
        thousands of files this evaluation never read.

        Adds watches before it waits, and removes none (module
        docstring). A directory found gone while adding is itself the
        change, answered without waiting.
        """
        forgotten = await self._add(_parents(self._watcher.watching()))
        if forgotten:
            return forgotten
        while True:
            event = await self._receive.receive()
            paths = self._paths(event)
            if not paths:
                continue
            for path in paths:
                forgotten += await self._watcher.changed(path)
            return sorted(set(forgotten))

    async def run(self) -> None:
        """`next_change()` forever. Cancel it to stop."""
        while True:
            await self.next_change()

    # -- events -----------------------------------------------------------
    def _paths(self, event: FileSystemEvent) -> list[str]:
        """Which watched files this event is about. Often none.

        Two shapes, and the second is easy to miss:

        - a FILE event is about one file, or two for a rename, and it
          matters only if some root depends on one of them;
        - a DIRECTORY that was deleted or moved takes every watched
          file under it at once.

        A directory that was only MODIFIED is not a change: watchdog
        sends one for the parent beside every file event, and taking it
        as "everything in here changed" would forget every root in the
        directory on each save.
        """
        if event.event_type not in CHANGES:
            return []
        named = {os.fsdecode(event.src_path)}
        if event.event_type == EVENT_TYPE_MOVED:
            named.add(os.fsdecode(event.dest_path))
        watched = self._watcher.watching()
        if event.is_directory:
            if event.event_type == EVENT_TYPE_MODIFIED:
                return []
            return [p for p in watched if os.path.dirname(p) in named]
        return [p for p in watched if p in named]


def _parents(files: list[str]) -> set[str]:
    """The directories to watch, for a set of files to watch.

    `Path.parent` and not `dirname`, so a bare name answers "." rather
    than the empty string that `add_watch` would refuse."""
    return {str(Path(p).parent) for p in files}
