#!/usr/bin/env agent-python
"""E2: E4's path with a libkrun router.

client (krun or UML) --unixgram socketpair--> router (krun): nft redirect,
  relay --vsock (libkrun's own device)--> egress (host) --> iperf3 -s

    lab/router/e2.py LAUNCHER VMLINUX KRUN_INITRD EGRESS IPERF3 [client kernel args...]

E2_CLIENT=uml:KERNEL:INITRD runs the client under UML instead.
E2_CPUS sets the router's vCPUs (default 2).
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

from e4 import tree_cpu, uml, wait_for

VSOCK_PORT = 5000
KRUN_ARGS = "console=ttyS0 reboot=k panic=-1 krun=1"


def krun(launcher: Path, vmlinux: Path, initrd: Path, cpus: int, fd: int, cmdline: str, *vsock: str) -> list[str]:
    mtu = os.environ.get("E4_MTU", "1500")
    return [
        str(launcher),
        str(vmlinux),
        str(initrd),
        f"{KRUN_ARGS} mtu={mtu} {cmdline}",
        str(cpus),
        "256",
        str(fd),
        *vsock,
    ]


def thread_cpu(pid: int) -> list[str]:
    """CPU seconds per thread: libkrun names its vCPU and device threads."""
    lines = []
    for task in sorted(Path(f"/proc/{pid}/task").iterdir()):
        fields = (task / "stat").read_text().rsplit(")", 1)[1].split()
        seconds = (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
        lines.append(f"{(task / 'comm').read_text().strip()} {seconds:.1f}")
    return lines


def collect_stats(stream: IO[bytes], stats: list[list[int]]) -> None:
    """The router's `lab: stat cpu ...` lines: /proc/stat jiffies."""
    for line in stream:
        text = line.decode(errors="replace")
        if "lab: stat cpu " in text:
            stats.append([int(field) for field in text.split("lab: stat cpu ", 1)[1].split()])


def guest_busy(stats: list[list[int]]) -> str:
    """Busy CPU seconds inside the router over the run: halt polling not counted."""
    if len(stats) < 2:
        return "no samples"
    first, last = stats[0], stats[-1]
    delta = [b - a for a, b in zip(first, last, strict=False)]
    idle = delta[3] + delta[4]
    busy = sum(delta[:8]) - idle
    # USER_HZ is 100 in every Linux build.
    return f"{busy / 100:.1f} s busy, {idle / 100:.1f} s idle, over {len(stats) - 1} samples 2 s apart"


def main() -> None:
    launcher, vmlinux, initrd, egress, iperf3 = (Path(arg) for arg in sys.argv[1:6])
    client_args = sys.argv[6:]
    run = Path(tempfile.mkdtemp(prefix="e2-"))
    uds = run / f"uds_{VSOCK_PORT}"
    procs: list[subprocess.Popen[bytes]] = []
    try:
        procs.append(subprocess.Popen([str(iperf3), "-s", "-B", "127.0.0.1", "-1"], stdout=subprocess.DEVNULL))
        procs.append(subprocess.Popen([str(egress), str(uds), "10.0.2.2"]))
        wait_for(uds)
        router_end, client_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        start = time.monotonic()
        router = subprocess.Popen(
            krun(
                launcher,
                vmlinux,
                initrd,
                int(os.environ.get("E2_CPUS", "2")),
                router_end.fileno(),
                "lab=router",
                str(VSOCK_PORT),
                str(uds),
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
            if "lab:" in text or "relay" in text or "rror" in text:
                print(f"[router] {text}")
            if "router ready" in text:
                print(f"router ready after {time.monotonic() - start:.2f} s")
                break
        else:
            raise RuntimeError("router exited before it was ready")
        stats: list[list[int]] = []
        threading.Thread(target=collect_stats, args=(router.stdout, stats), daemon=True).start()
        client_spec = os.environ.get("E2_CLIENT", "")
        if client_spec.startswith("uml:"):
            _, kernel, uml_initrd = client_spec.split(":")
            command = uml(
                Path(kernel), Path(uml_initrd), run, "client", client_end.fileno(), "lab=client", *client_args
            )
        else:
            command = krun(launcher, vmlinux, initrd, 2, client_end.fileno(), " ".join(["lab=client", *client_args]))
        client = subprocess.run(
            command,
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
        for name, proc in zip(("iperf3", "egress", "router"), procs, strict=True):
            print(f", {name} {tree_cpu(proc.pid):.1f}", end="")
        print()
        print("router threads:", ", ".join(thread_cpu(router.pid)))
        print(f"router guest busy: {guest_busy(stats)}")
    finally:
        for proc in reversed(procs):
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    os.setpgrp()
    main()
