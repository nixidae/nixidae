"""Per-stream async byte buffer for ttrpc."""

import asyncio
from typing import Optional

from grpclib.exceptions import StreamTerminatedError


class StreamBuffer:
    """asyncio.Queue-based buffer for one ttrpc stream's incoming payloads.

    The protocol layer calls :py:meth:`feed_data` / :py:meth:`feed_eof` /
    :py:meth:`feed_error` from the event-loop thread (always synchronous).
    The handler task calls :py:meth:`read_message` which awaits the queue.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        self._error: Optional[Exception] = None

    def feed_data(self, data: bytes) -> None:
        """Push a raw payload into the queue (called by protocol layer)."""
        self._queue.put_nowait(data)

    def feed_eof(self) -> None:
        """Signal end of stream by pushing a None sentinel."""
        self._queue.put_nowait(None)

    def feed_error(self, exc: Exception) -> None:
        """Record a connection error; subsequent reads will raise it."""
        self._error = exc
        # wake up any waiter immediately so it sees the error
        self._queue.put_nowait(None)

    async def read_message(self) -> Optional[bytes]:
        """Block until a payload is available.

        Returns ``None`` at EOF, raises :py:exc:`StreamTerminatedError` on
        connection error.
        """
        item = await self._queue.get()
        if item is None and self._error is not None:
            raise StreamTerminatedError(str(self._error)) from self._error
        return item
