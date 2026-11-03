#!/usr/bin/env agent-python
"""Send human-monitor commands to a QEMU QMP socket; print each reply."""

from __future__ import annotations

import json
import socket
import sys


def main() -> None:
    path, *commands = sys.argv[1:]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    reader = sock.makefile("r")
    reader.readline()

    def call(execute: str, arguments: dict[str, object] | None = None) -> object:
        message: dict[str, object] = {"execute": execute}
        if arguments:
            message["arguments"] = arguments
        sock.sendall(json.dumps(message).encode() + b"\n")
        while True:
            reply = json.loads(reader.readline())
            if "return" in reply or "error" in reply:
                return reply

    call("qmp_capabilities")
    for command in commands:
        reply = call("human-monitor-command", {"command-line": command})
        print(f"== {command}")
        print(reply["return"] if isinstance(reply, dict) and "return" in reply else reply)


if __name__ == "__main__":
    main()
