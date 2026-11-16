#!/usr/bin/env agent-python
"""E4: TCP from a client guest through the router to the host.

client (UML) --vec0 socketpair--> router (UML): nft redirect, relay
  --vsock (virtio_uml, vhost-device-vsock)--> egress (host) --> iperf3 -s

    lab/router/e4.py KERNEL INITRD VHOST_DEVICE_VSOCK EGRESS IPERF3 [client kernel args...]
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VSOCK_PORT = 5000


def uml(kernel: Path, initrd: Path, run: Path, name: str, fd: int, *extra: str) -> list[str]:
    return [
        str(kernel),
        "mem=128M",
        f"initrd={initrd}",
        f"vec0:transport=fd,fd={fd},depth=128,mtu={os.environ.get('E4_MTU', '1500')}",
        "con=null",
        "con0=fd:0,fd:1",
        "seccomp=auto",
        f"uml_dir={run}",
        f"umid={name}",
        *extra,
    ]


def wait_for(path: Path) -> None:
    deadline = time.monotonic() + 10
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(path)
        time.sleep(0.05)


def main() -> None:
    kernel, initrd, vhost, egress, iperf3 = (Path(arg) for arg in sys.argv[1:6])
    run = Path(tempfile.mkdtemp(prefix="e4-"))
    device = run / "vsock.sock"
    uds = run / "uds"
    procs: list[subprocess.Popen[bytes]] = []
    try:
        procs.append(subprocess.Popen([str(iperf3), "-s", "-B", "127.0.0.1", "-1"], stdout=subprocess.DEVNULL))
        procs.append(subprocess.Popen([str(egress), f"{uds}_{VSOCK_PORT}", "10.0.2.2"]))
        procs.append(subprocess.Popen([str(vhost), "--vm", f"guest-cid=3,socket={device},uds-path={uds}"]))
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
            text = line.decode(errors="replace").rstrip()
            if "lab:" in text or "relay" in text:
                print(f"[router] {text}")
            if "router ready" in text:
                break
        client = subprocess.run(
            uml(kernel, initrd, run, "client", client_end.fileno(), "lab=client", *sys.argv[6:]),
            pass_fds=(client_end.fileno(),),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=120,
            check=False,
        )
        for line in client.stdout.decode(errors="replace").splitlines():
            if any(word in line for word in ("lab:", "sec", "iperf", "error", "Connecting")):
                print(f"[client] {line}")
    finally:
        for proc in reversed(procs):
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    os.setpgrp()
    main()
