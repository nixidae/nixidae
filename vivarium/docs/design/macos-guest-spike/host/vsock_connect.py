#!/usr/bin/env agent-python
"""Connect to a guest vsock port through virtio-vsock-unix's host socket.

    vsock_connect.py SOCKET PORT TEXT      send TEXT, print the reply
    vsock_connect.py SOCKET PORT --bulk N  send N bytes, time the echo

The device's handshake: write "CONNECT <port>\\n", read "OK <host port>\\n".
"""

from __future__ import annotations

import socket
import sys
import threading
import time


def connect(path: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    sock.sendall(f"CONNECT {port}\n".encode())
    line = b""
    while not line.endswith(b"\n"):
        chunk = sock.recv(1)
        if not chunk:
            raise SystemExit(f"no OK from the device (got {line!r}): nothing listens on port {port}?")
        line += chunk
    if not line.startswith(b"OK "):
        raise SystemExit(f"unexpected handshake reply {line!r}")
    print(f"handshake: {line.decode().strip()}", file=sys.stderr)
    return sock


def read_all(sock: socket.socket) -> bytes:
    data = bytearray()
    while chunk := sock.recv(65536):
        data += chunk
    return bytes(data)


def main() -> None:
    path, port = sys.argv[1], int(sys.argv[2])
    sock = connect(path, port)
    if sys.argv[3] == "--bulk":
        total = int(sys.argv[4])
        payload = b"x" * total

        def send() -> None:
            sock.sendall(payload)
            sock.shutdown(socket.SHUT_WR)

        start = time.monotonic()
        sender = threading.Thread(target=send)
        sender.start()
        got = read_all(sock)
        sender.join()
        seconds = time.monotonic() - start
        print(f"echoed {len(got)} of {total} bytes in {seconds:.3f} s, {len(got) / seconds / 1e6:.1f} MB/s each way")
        raise SystemExit(0 if got == payload else 1)
    sock.sendall(sys.argv[3].encode())
    sock.shutdown(socket.SHUT_WR)
    print(repr(read_all(sock)))


if __name__ == "__main__":
    main()
