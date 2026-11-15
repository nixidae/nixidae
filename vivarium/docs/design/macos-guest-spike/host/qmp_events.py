#!/usr/bin/env agent-python
"""Print QMP events from a socket for a number of seconds."""

from __future__ import annotations

import json
import select
import socket
import sys
import time


def main() -> None:
    path, seconds = sys.argv[1], float(sys.argv[2])
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    sock.sendall(b'{"execute": "qmp_capabilities"}\n')
    start = time.monotonic()
    buffer = b""
    while time.monotonic() - start < seconds:
        ready, _, _ = select.select([sock], [], [], 1.0)
        if not ready:
            continue
        chunk = sock.recv(65536)
        if not chunk:
            break
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            message = json.loads(line)
            if "event" in message:
                print(f"t={time.monotonic() - start:5.1f}s", message["event"], json.dumps(message.get("data", {})), flush=True)


if __name__ == "__main__":
    main()
