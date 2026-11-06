#!/usr/bin/env agent-python
"""Save every TCP connection on each given port to its own file.

    collect.py OUTDIR PORT...

The guest reaches the host at 10.0.2.2 through slirp. A file is named
`<port>-<n>.log`; bytes are written as they arrive, so the tail of a
connection that a guest reset cuts off is kept.
"""

from __future__ import annotations

import itertools
import pathlib
import sys

import anyio
import anyio.abc

counter = itertools.count()


async def serve(outdir: pathlib.Path, port: int) -> None:
    async def handle(stream: anyio.abc.SocketStream) -> None:
        path = outdir / f"{port}-{next(counter):04d}.log"
        async with stream:
            with path.open("wb") as out:
                try:
                    async for chunk in stream:
                        out.write(chunk)
                        out.flush()
                except anyio.BrokenResourceError:
                    pass

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=port)
    await listener.serve(handle)


async def main(outdir: pathlib.Path, ports: list[int]) -> None:
    async with anyio.create_task_group() as group:
        for port in ports:
            group.start_soon(serve, outdir, port)


if __name__ == "__main__":
    out = pathlib.Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    anyio.run(main, out, [int(port) for port in sys.argv[2:]])
