#!/usr/bin/env agent-python
"""Count vsock handshakes a builder guest leaves unanswered.

    handshake_count.py SNAP_DIR RUN_DIR [COUNT]

Starts a guest from SNAP_DIR, then makes COUNT (default 200) daemon
handshakes one after another, each with darwin_builder's 2 s limit.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import anyio

sys.path.insert(0, str(Path(__file__).resolve().parent))
import darwin_builder as db  # noqa: E402 -- the spike tool next to this file


async def main() -> None:
    snap, run = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 200
    silent = refused = answered = 0
    slowest = 0.0
    async with db.started(snap, run):
        for _ in range(count):
            t0 = time.monotonic()
            try:
                stream = await db.connect_guest(run / "vsock.sock", db.DAEMON_PORT)
            except TimeoutError:
                silent += 1
                continue
            except (OSError, anyio.EndOfStream):
                # The device closes the host side when the guest resets the request.
                refused += 1
                continue
            await stream.aclose()
            answered += 1
            slowest = max(slowest, time.monotonic() - t0)
    print(json.dumps({"snapshot": snap.name, "answered": answered, "silent": silent, "refused": refused,
                      "slowest_answer_s": round(slowest, 3)}))  # fmt: skip


if __name__ == "__main__":
    anyio.run(main)
