#!/usr/bin/env agent-python
"""E4: TCP from a client guest through the router to the host.

client (UML) --vec0 socketpair--> router (UML): nft redirect, relay
  --vsock (virtio_uml, vhost-device-vsock)--> egress (host) --> iperf3 -s

    lab/router/e4.py KERNEL INITRD VHOST_DEVICE_VSOCK EGRESS IPERF3 [client kernel args...]

VHOST_DEVICE_VSOCK `-` makes the egress serve the vsock device itself.
"""

from __future__ import annotations

import os
import resource
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


def tree_cpu(pid: int) -> float:
    """CPU seconds of *pid* and its live descendants: UML runs guest
    processes as host children of the kernel process."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return 0.0
    total = (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    for task in Path(f"/proc/{pid}/task").glob("*"):
        children = task / "children"
        if children.exists():
            total += sum(tree_cpu(int(child)) for child in children.read_text().split())
    return total


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
    names = ["iperf3", "egress"]
    try:
        procs.append(subprocess.Popen([str(iperf3), "-s", "-B", "127.0.0.1", "-1"], stdout=subprocess.DEVNULL))
        if str(vhost) == "-":
            procs.append(subprocess.Popen([str(egress), f"vhost-user:{device}", "10.0.2.2"]))
        else:
            procs.append(subprocess.Popen([str(egress), f"{uds}_{VSOCK_PORT}", "10.0.2.2"]))
            procs.append(subprocess.Popen([str(vhost), "--vm", f"guest-cid=3,socket={device},uds-path={uds}"]))
            names.append("vhost")
        names.append("router")
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
        client_cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
        print(f"CPU s: client {client_cpu.ru_utime + client_cpu.ru_stime:.1f}", end="")
        for name, proc in zip(names, procs, strict=True):
            print(f", {name} {tree_cpu(proc.pid):.1f}", end="")
        print()
    finally:
        for proc in reversed(procs):
            proc.kill()
            proc.wait()
            # The egress prints its device counters when the router goes.
            time.sleep(0.2)


if __name__ == "__main__":
    os.setpgrp()
    main()
