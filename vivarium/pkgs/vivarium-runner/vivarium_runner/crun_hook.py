"""crun's `createRuntime` hook: the container's init starts with its links.

crun runs it after it made the container's namespaces and before it starts
the init. It hands the init's pid to the launcher (`container.main`), which
makes each segment link in the container, and it returns when the launcher
says they exist. A failure ends the container before its init runs.

A script, run with ``-I -S``: it imports nothing of vivarium_runner, so
the hook costs one bare interpreter.
"""

from __future__ import annotations

import json
import socket
import sys


def main() -> int:
    # crun writes the OCI state on stdin; its shape is the runtime spec's.
    pid = int(json.load(sys.stdin)["pid"])
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(sys.argv[1])
        conn.sendall(f"{pid}\n".encode())
        return 0 if conn.recv(1) == b"1" else 1


raise SystemExit(main())
