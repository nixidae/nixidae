#!/usr/bin/env agent-python
"""E5: ICMP echo and UDP from a krun guest through the krun router.

    lab/router/e5.py LAUNCHER VMLINUX KRUN_INITRD EGRESS IPERF3 [client kernel args...]

E5_ROUTER adds kernel arguments to the router (`noicmp=1`: the negative
control). The host runs a UDP echo on 127.0.0.1:7777, which the guest
reaches as 10.0.2.2.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from e2 import VSOCK_PORT, krun
from e4 import wait_for


def udp_echo(sock: socket.socket) -> None:
    while True:
        data, peer = sock.recvfrom(65536)
        sock.sendto(data, peer)


def main() -> None:
    launcher, vmlinux, initrd, egress, iperf3 = (Path(arg) for arg in sys.argv[1:6])
    run = Path(tempfile.mkdtemp(prefix="e5-"))
    uds = run / f"uds_{VSOCK_PORT}"
    echo = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    echo.bind(("127.0.0.1", 7777))
    threading.Thread(target=udp_echo, args=(echo,), daemon=True).start()
    procs: list[subprocess.Popen[bytes]] = []
    try:
        procs.append(subprocess.Popen([str(iperf3), "-s", "-B", "127.0.0.1"], stdout=subprocess.DEVNULL))
        procs.append(subprocess.Popen([str(egress), str(uds), "10.0.2.2"]))
        wait_for(uds)
        router_end, client_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        router_args = " ".join(["lab=router", os.environ.get("E5_ROUTER", "")])
        router = subprocess.Popen(
            krun(launcher, vmlinux, initrd, 2, router_end.fileno(), router_args, str(VSOCK_PORT), str(uds)),
            pass_fds=(router_end.fileno(),),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        procs.append(router)
        if router.stdout is None:
            raise RuntimeError("no router stdout")
        for line in router.stdout:
            text = line.decode(errors="replace").rstrip()
            if ("lab:" in text and "stat" not in text) or "relay" in text or "rror" in text:
                print(f"[router] {text}")
            if "router ready" in text:
                break
        else:
            raise RuntimeError("router exited before it was ready")
        client = subprocess.run(
            krun(launcher, vmlinux, initrd, 2, client_end.fileno(), " ".join(["lab=e5", *sys.argv[6:]])),
            pass_fds=(client_end.fileno(),),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=120,
            check=False,
        )
        for line in client.stdout.decode(errors="replace").splitlines():
            if any(word in line for word in ("lab:", "bytes from", "packet", "sec", "iperf", "error", "round-trip")):
                print(f"[client] {line}")
    finally:
        for proc in reversed(procs):
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    os.setpgrp()
    main()
