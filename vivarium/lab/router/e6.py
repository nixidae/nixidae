#!/usr/bin/env python3
"""E6: a forward. The host's iperf3 reaches a guest's server through
the router: host port, egress, vsock (libkrun listens on a unix socket),
relay, guest.

    lab/router/e6.py LAUNCHER VMLINUX KRUN_INITRD EGRESS IPERF3 [iperf3 client args...]

LAUNCHER `uml` runs both guests under UML instead: VMLINUX is the UML
kernel, KRUN_INITRD the UML initrd, and the egress serves the router's
vsock device and opens the forward's connection through it.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from e2 import VSOCK_PORT, krun
from e4 import uml, wait_for

IN_PORT = 5001
HOST_PORT = 5202


def wait_line(proc: subprocess.Popen[bytes], marker: str, tag: str) -> None:
    if proc.stdout is None:
        raise RuntimeError(f"no {tag} stdout")
    for line in proc.stdout:
        text = line.decode(errors="replace").rstrip()
        if ("lab:" in text and "stat" not in text) or "relay" in text or "rror" in text:
            print(f"[{tag}] {text}")
        if marker in text:
            return
    raise RuntimeError(f"{tag} exited before '{marker}'")


def main() -> None:
    launcher, vmlinux, initrd, egress, iperf3 = (Path(arg) for arg in sys.argv[1:6])
    run = Path(tempfile.mkdtemp(prefix="e6-"))
    uds = run / f"uds_{VSOCK_PORT}"
    inbound = run / f"in_{IN_PORT}"
    device = run / "vsock.sock"
    on_uml = str(launcher) == "uml"
    procs: list[subprocess.Popen[bytes]] = []
    try:
        if on_uml:
            listen, forward = f"vhost-user:{device}", f"vsock:{IN_PORT}"
        else:
            listen, forward = str(uds), str(inbound)
        procs.append(subprocess.Popen([str(egress), listen, "10.0.2.2", f"{HOST_PORT}=10.0.2.15:5201@{forward}"]))
        wait_for(device if on_uml else uds)
        router_end, guest_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        if on_uml:
            router_argv = uml(
                vmlinux, initrd, run, "router", router_end.fileno(), "lab=router", f"virtio_uml.device={device}:19"
            )
            guest_argv = uml(vmlinux, initrd, run, "guest", guest_end.fileno(), "lab=server")
        else:
            router_argv = krun(
                launcher,
                vmlinux,
                initrd,
                2,
                router_end.fileno(),
                "lab=router",
                str(VSOCK_PORT),
                str(uds),
                str(IN_PORT),
                str(inbound),
            )
            guest_argv = krun(launcher, vmlinux, initrd, 2, guest_end.fileno(), "lab=server")
        router = subprocess.Popen(
            router_argv,
            pass_fds=(router_end.fileno(),),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        procs.append(router)
        wait_line(router, "router ready", "router")
        guest = subprocess.Popen(
            guest_argv,
            pass_fds=(guest_end.fileno(),),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        procs.append(guest)
        wait_line(guest, "server ready", "guest")
        # iperf3 -s in the guest starts a moment after the line.
        result = subprocess.run(
            [
                str(iperf3),
                "-c",
                "127.0.0.1",
                "-p",
                str(HOST_PORT),
                "-t",
                "10",
                "--connect-timeout",
                "5000",
                *sys.argv[6:],
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
        for line in result.stdout.decode(errors="replace").splitlines():
            if any(word in line for word in ("sender", "receiver", "error")):
                print(f"[host] {line}")
        if result.returncode != 0:
            print(f"[host] iperf3 exit {result.returncode}: {result.stderr.decode(errors='replace').strip()}")
    finally:
        for proc in reversed(procs):
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    os.setpgrp()
    main()
