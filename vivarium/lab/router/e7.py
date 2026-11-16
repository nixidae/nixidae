#!/usr/bin/env agent-python
"""E7: content through the egress's own vsock device, both ways.

The client guest sends `seq 1 COUNT` to the host's port 7000, then
reads the same stream from port 7001 and prints its MD5. The host
checks both against its own. E7_STALL seconds before the host reads
port 7000 fill the device's spill.

    lab/router/e7.py KERNEL INITRD EGRESS COUNT
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from e4 import uml, wait_for


def receive(server: socket.socket, stall: float, result: dict[str, str]) -> None:
    conn, _ = server.accept()
    time.sleep(stall)
    digest = hashlib.md5()
    total = 0
    while chunk := conn.recv(1 << 20):
        digest.update(chunk)
        total += len(chunk)
    result["received"] = f"{digest.hexdigest()} {total} bytes"


def send(server: socket.socket, data: bytes) -> None:
    conn, _ = server.accept()
    conn.sendall(data)
    conn.close()


def listen(port: int) -> socket.socket:
    return socket.create_server(("127.0.0.1", port))


def main() -> None:
    kernel, initrd, egress = (Path(arg) for arg in sys.argv[1:4])
    count = int(sys.argv[4])
    stall = float(os.environ.get("E7_STALL", "0"))
    data = "".join(f"{i}\n" for i in range(1, count + 1)).encode()
    expected = f"{hashlib.md5(data).hexdigest()} {len(data)} bytes"
    run = Path(tempfile.mkdtemp(prefix="e7-"))
    device = run / "vsock.sock"
    result: dict[str, str] = {}
    threads = [
        threading.Thread(target=receive, args=(listen(7000), stall, result), daemon=True),
        threading.Thread(target=send, args=(listen(7001), data), daemon=True),
    ]
    for thread in threads:
        thread.start()
    procs: list[subprocess.Popen[bytes]] = []
    try:
        procs.append(subprocess.Popen([str(egress), f"vhost-user:{device}", "10.0.2.2"]))
        wait_for(device)
        router_end, client_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        router = subprocess.Popen(
            uml(kernel, initrd, run, "router", router_end.fileno(), "lab=router", f"virtio_uml.device={device}:19"),
            pass_fds=(router_end.fileno(),),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        procs.append(router)
        if router.stdout is None:
            raise RuntimeError("no router stdout")
        for line in router.stdout:
            if b"router ready" in line:
                break
        client = subprocess.run(
            uml(kernel, initrd, run, "client", client_end.fileno(), "lab=verify", f"count={count}"),
            pass_fds=(client_end.fileno(),),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=300,
            check=False,
        )
        got = ""
        for line in client.stdout.decode(errors="replace").splitlines():
            if "lab: verify" in line:
                print(f"[client] {line}")
            if "lab: verify got" in line:
                got = line.split("got", 1)[1].split()[0]
        threads[0].join(timeout=10)
        print(f"expected  {expected}")
        print(f"to host   {result.get('received', 'nothing')}: {'match' if result.get('received') == expected else 'MISMATCH'}")
        print(f"to guest  {got}: {'match' if got == expected.split()[0] else 'MISMATCH'}")
    finally:
        for proc in reversed(procs):
            proc.kill()
            proc.wait()
            time.sleep(0.2)


if __name__ == "__main__":
    os.setpgrp()
    main()
