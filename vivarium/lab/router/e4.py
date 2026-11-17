#!/usr/bin/env agent-python
"""E4: TCP from a client guest through the router to the host.

client (UML) --vec0 socketpair--> router (UML): nft redirect, relay
  --vsock (virtio_uml, vhost-device-vsock)--> egress (host) --> iperf3 -s

    lab/router/e4.py KERNEL INITRD VHOST_DEVICE_VSOCK EGRESS IPERF3 [client kernel args...]

VHOST_DEVICE_VSOCK `-` makes the egress serve the vsock device itself.
E4_ROUTER adds router kernel arguments, such as `vsockbuf=4194304`.
"""

from __future__ import annotations

import os
import resource
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import IO

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


def collect_stats(stream: IO[bytes], stats: dict[str, list[list[int]]], irqs: dict[str, list[str]]) -> None:
    """The router's `lab: stat cpu...` lines (/proc/stat jiffies, per CPU and in all)
    and `lab: irq` lines (its virtio rows of /proc/interrupts)."""
    for line in stream:
        text = line.decode(errors="replace")
        if "lab: stat cpu" in text:
            name, *fields = text.split("lab: stat ", 1)[1].split()
            stats.setdefault(name, []).append([int(field) for field in fields])
        elif "lab: irq " in text:
            row = text.split("lab: irq ", 1)[1].strip()
            irqs.setdefault(row.split()[0], []).append(row)
        elif "lab: task " in text:
            row = text.split("lab: task ", 1)[1].strip()
            irqs.setdefault(f"task {row.split()[0]}", []).append(row)


def guest_busy(samples: list[list[int]]) -> str:
    """Busy CPU seconds inside the router over the run: halt polling not counted."""
    if len(samples) < 2:
        return "no samples"
    first, last = samples[0], samples[-1]
    delta = [b - a for a, b in zip(first, last, strict=False)]
    idle = delta[3] + delta[4]
    busy = sum(delta[:8]) - idle
    # USER_HZ is 100 in every Linux build. Fields: user nice system idle iowait irq softirq steal.
    return (
        f"{busy / 100:.1f} s busy (user {delta[0] / 100:.1f}, sys {delta[2] / 100:.1f},"
        f" irq {delta[5] / 100:.1f}, softirq {delta[6] / 100:.1f}, steal {delta[7] / 100:.1f}), {idle / 100:.1f} s idle, over {len(samples) - 1} samples 2 s apart"
    )


def print_stats(stats: dict[str, list[list[int]]], irqs: dict[str, list[str]]) -> None:
    for name, samples in sorted(stats.items()):
        print(f"router guest {name}: {guest_busy(samples)}")
    for name, rows in irqs.items():
        print(f"router {name} first: {rows[0]}")
        print(f"router {name} last:  {rows[-1]}")


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
            uml(
                kernel,
                initrd,
                run,
                "router",
                router_end.fileno(),
                f"lab=router {os.environ.get('E4_ROUTER', '')}",
                f"virtio_uml.device={device}:19",
            ),
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
        stats: dict[str, list[list[int]]] = {}
        irqs: dict[str, list[str]] = {}
        threading.Thread(target=collect_stats, args=(router.stdout, stats, irqs), daemon=True).start()
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
        print_stats(stats, irqs)
    finally:
        for proc in reversed(procs):
            proc.kill()
            proc.wait()
            # The egress prints its device counters when the router goes.
            time.sleep(0.2)


if __name__ == "__main__":
    os.setpgrp()
    main()
