#!/usr/bin/env agent-python
"""Round-trip bytes through a QEMU chardev unix socket.

    serial_roundtrip.py SOCKET SEND_TEXT SECONDS

Sends SEND_TEXT, then prints whatever arrives for SECONDS.
"""

from __future__ import annotations

import socket
import sys
import time


def main() -> None:
    path, text, seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    sock.settimeout(0.5)
    sock.sendall(text.encode())
    deadline = time.monotonic() + seconds
    received = b""
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            continue
        if not chunk:
            break
        received += chunk
    print(repr(received))


if __name__ == "__main__":
    main()
