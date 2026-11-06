#!/usr/bin/env agent-python
"""Echo every connection on a unix socket: the host end of guest -> host vsock.

    unix_echo.py PATH

virtio-vsock-unix connects to PATH when the guest connects to CID 2 at the
port PATH ends in (`<device path>_<port>`).
"""

from __future__ import annotations

import pathlib
import sys

import anyio
import anyio.abc


async def echo(stream: anyio.abc.SocketStream) -> None:
    async with stream:
        try:
            async for chunk in stream:
                await stream.send(chunk)
        except anyio.BrokenResourceError:
            pass


async def main(path: pathlib.Path) -> None:
    listener = await anyio.create_unix_listener(path)
    await listener.serve(echo)


if __name__ == "__main__":
    target = pathlib.Path(sys.argv[1])
    target.unlink(missing_ok=True)
    anyio.run(main, target)
