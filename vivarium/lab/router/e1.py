#!/usr/bin/env python3
"""E1: vsock throughput from a UML guest to the host.

vhost-device-vsock serves the guest's virtio-vsock over vhost-user.
The guest streams zeros to host port 5000; vhost-device-vsock connects
each guest stream to `<uds>_5000`, where this script counts the bytes.

    lab/router/e1.py KERNEL INITRD VHOST_DEVICE_VSOCK
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

PORT = 5000


def rss_kib(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return 0


def cpu_seconds(pid: int) -> float:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def main() -> None:
    kernel, initrd, vhost = (Path(arg) for arg in sys.argv[1:4])
    run = Path(tempfile.mkdtemp(prefix="e1-"))
    device = run / "vsock.sock"
    uds = run / "uds"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(f"{uds}_{PORT}")
    listener.listen(1)

    vm = ",".join(filter(None, [f"guest-cid=3,socket={device},uds-path={uds}", os.environ.get("E1_VM", "")]))
    backend = subprocess.Popen([str(vhost), "--vm", vm])
    while not device.exists():
        time.sleep(0.05)

    started = time.monotonic()
    guest = subprocess.Popen(
        [
            str(kernel),
            "mem=128M",
            f"initrd={initrd}",
            "lab=vsock-tx",
            f"virtio_uml.device={device}:19",
            "con=null",
            "con0=fd:0,fd:1",
            "seccomp=auto",
            f"uml_dir={run}",
            "umid=e1",
            *sys.argv[4:],
        ],
        stdin=subprocess.DEVNULL,
    )

    counted = 0
    peak = 0

    def sample() -> None:
        nonlocal peak
        while guest.poll() is None:
            # The kernel process alone: every UML process maps the same
            # memory file, so a sum counts it once per process.
            peak = max(peak, rss_kib(guest.pid))
            time.sleep(0.2)

    threading.Thread(target=sample, daemon=True).start()
    listener.settimeout(30)
    try:
        conn, _ = listener.accept()
    except TimeoutError:
        guest.kill()
        print(f"no connection; vhost-device-vsock exit {backend.poll()}")
        raise
    first = time.monotonic()
    conn.settimeout(30)
    try:
        while chunk := conn.recv(1 << 20):
            counted += len(chunk)
    except TimeoutError:
        guest.kill()
        print(f"stalled after {counted / 2**20:.0f} MiB; vhost-device-vsock exit {backend.poll()}")
        raise
    done = time.monotonic()
    guest.wait(timeout=60)
    backend_cpu = cpu_seconds(backend.pid)
    guest_cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
    backend.terminate()
    print(f"CPU: guest {guest_cpu.ru_utime + guest_cpu.ru_stime:.1f} s, vhost-device-vsock {backend_cpu:.1f} s")
    seconds = done - first
    print(f"boot to first byte: {first - started:.2f} s")
    print(f"{counted / 2**20:.0f} MiB in {seconds:.2f} s = {counted * 8 / seconds / 1e9:.2f} Gbit/s")
    print(f"peak RSS of the UML processes: {peak / 1024:.0f} MiB")


if __name__ == "__main__":
    main()
