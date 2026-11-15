#!/usr/bin/env agent-python
"""snap_vfs.py save|lazy|eager SNAP: snapshots of a guest with a vhost-user-fs device.

save   boot sonoma-nix on memory-backend-shm with vhost-user-fs, wait for the
       daemon, save everything to SNAP/full.bin, split it into ram.bin + state.bin
lazy   start from a clone of ram.bin mapped shared (x-ignore-shared state.bin)
eager  start by loading full.bin into memory-backend-shm

ZEROFREE=1 zeroes the guest's free pages before the save. LIVE=1 saves the
guest while it runs, so dirty logging is on. MULTIFD_CHANNELS=N
saves in the mapped-ram format, which an eager start loads with N threads;
such a snapshot has no ram.bin.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

import anyio

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import darwin_builder as db  # noqa: E402

IMG = Path(os.environ.get("IMG", Path.home() / "Dynamist/orchard-images/sonoma"))
GUEST = Path(os.environ.get("GUEST", Path.home() / "Dynamist/orchard-images/sonoma-nix"))
VFSD = Path(os.environ.get("VFSD", "darwin-virtiofsd"))
SSH_PORT = 22299
# 0: QEMU's stream format. N: mapped-ram, loaded by N multifd channels.
MULTIFD_CHANNELS = int(os.environ.get("MULTIFD_CHANNELS", "0"))
BENCH = """
for i in $(seq 30); do test -d /nix/store && break; sleep 1; done
mount_virtiofs hoststore /nix/store || exit 1
W=/nix/store/v1zb8pqd25hsj0wd748nfczsa8ygciba-clang-wrapper-21.1.8
cd /tmp
for round in 1 2 3; do
  s=$(perl -MTime::HiRes=time -e 'print time')
  for i in 1 2 3 4 5 6 7 8 9 10; do "$W/bin/cc" -c /tmp/bench.c -o /tmp/bench.o || exit 1; done
  e=$(perl -MTime::HiRes=time -e 'print time')
  echo "compile10 round $round: $(echo "$e - $s" | bc) s"
done
umount /nix/store
"""


def log(**fields: object) -> None:
    db.log(**fields)


@contextlib.asynccontextmanager
async def server(run: Path) -> AsyncIterator[Path]:
    sock = run / "vfs.sock"
    out = await anyio.open_file(run / "vfsd.log", "ab")
    async with await anyio.open_process(
        [str(VFSD), "--socket-path", str(sock), "--shared-dir", "/nix/store"],
        stdout=out.wrapped, stderr=out.wrapped, env=os.environ | {"RUST_LOG": "info"},
    ) as proc:  # fmt: skip
        with anyio.fail_after(5):
            while not await anyio.Path(sock).exists():
                await anyio.sleep(0.01)
        try:
            yield sock
        finally:
            with anyio.CancelScope(shield=True):
                if proc.returncode is None:
                    proc.terminate()
                await proc.wait()


def args(run: Path, ram: str, sock: Path, *, incoming: bool) -> list[str]:
    a = db.qemu_args(IMG, str(run), ram, ssh_port=SSH_PORT)
    a += ["-chardev", f"socket,id=vfs,path={sock}", "-device", "vhost-user-fs-pci,chardev=vfs,tag=hoststore"]
    return [*a, "-incoming", "defer"] if incoming else a


async def fresh_overlay(run: Path, backing: Path) -> None:
    await anyio.Path(run).mkdir(parents=True, exist_ok=True)
    await anyio.run_process(["cp", "-c", str(GUEST / "aux.img"), str(run / "aux.img")])
    await anyio.run_process([str(db.QEMU_IMG), "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b",
                             str(backing), str(run / "overlay.qcow2")])  # fmt: skip


async def save(snap: Path) -> None:
    await anyio.Path(snap).mkdir(parents=True)
    # The snapshot's disk: the guest's overlay as it is now, read only from here on.
    await anyio.run_process(["cp", "-c", str(GUEST / "overlay.qcow2"), str(snap / "disk.qcow2")])
    boot = snap / "boot"
    await fresh_overlay(boot, snap / "disk.qcow2")
    async with server(boot) as sock:
        vm = await db.launch(boot, args(boot, "memory-backend-shm", sock, incoming=False))
        qmp = None
        try:
            qmp = await db.Qmp.connect(boot / "qmp.sock")
            t0 = time.monotonic()
            with anyio.fail_after(300):
                while not await db.daemon_answers(boot / "vsock.sock"):
                    await anyio.sleep(0.5)
            log(event="daemon", seconds=round(time.monotonic() - t0, 1))
            await anyio.sleep(20)
            if os.environ.get("ZEROFREE") == "1":
                await zero_free(boot)
            await db.set_clock(boot / "vsock.sock")
            # LIVE=1 saves a running guest: QEMU and the server then log dirty pages.
            if os.environ.get("LIVE") != "1":
                await qmp.call("stop")
            t0 = time.monotonic()
            if MULTIFD_CHANNELS:
                await mapped_ram(qmp, MULTIFD_CHANNELS)
                # A load must use the save's channel count.
                await anyio.Path(snap / "mapped").write_text(f"{MULTIFD_CHANNELS}\n")
            await db.migrate(qmp, snap / "full.bin")
            log(event="saved", seconds=round(time.monotonic() - t0, 1))
        finally:
            await db.stop(vm, qmp)
    # The disk the saved RAM matches is the boot overlay; it becomes the base.
    await anyio.Path(boot / "overlay.qcow2").rename(snap / "overlay.qcow2")
    await anyio.Path(boot / "aux.img").rename(snap / "aux.img")
    if not MULTIFD_CHANNELS:
        await split(snap)


async def zero_free(boot: Path) -> None:
    """guest/zerofree.c, built here and run in the guest over SSH."""
    binary = boot / "zerofree"
    await anyio.run_process(["/usr/bin/clang", "-O2", "-mmacosx-version-min=14.0", "-o", str(binary),
                             str(HERE.parent / "guest/zerofree.c")])  # fmt: skip
    with anyio.fail_after(120):
        while not await db.ssh(boot, SSH_PORT, "true", check=False, connect_timeout=5):
            await anyio.sleep(1)
    await db.ssh(boot, SSH_PORT, "--", str(binary), "/tmp/")
    await db.ssh(boot, SSH_PORT, "sudo -n /tmp/zerofree")


async def mapped_ram(qmp: db.Qmp, channels: int) -> None:
    """RAM at fixed offsets in the file, zero pages left out, loaded by several threads."""
    await qmp.call("migrate-set-capabilities", capabilities=[
        {"capability": "mapped-ram", "state": True},
        {"capability": "multifd", "state": True},
    ])  # fmt: skip
    await qmp.call("migrate-set-parameters", **{"multifd-channels": channels})


async def split(snap: Path) -> None:
    """full.bin into ram.bin (a shared file) and state.bin (x-ignore-shared)."""
    run = snap / "split"
    await anyio.Path(run).mkdir()
    await anyio.run_process(["cp", "-c", str(snap / "aux.img"), str(run / "aux.img")])
    await anyio.run_process(["cp", "-c", str(snap / "overlay.qcow2"), str(run / "overlay.qcow2")])
    t0 = time.monotonic()
    ram = f"memory-backend-file,mem-path={snap / 'ram.bin'},share=on"
    async with server(run) as sock:
        vm = await db.launch(run, args(run, ram, sock, incoming=True))
        qmp = None
        try:
            qmp = await db.Qmp.connect(run / "qmp.sock")
            await qmp.call("migrate-incoming", uri=f"file:{snap / 'full.bin'}")
            while await qmp.status() == "inmigrate":
                await anyio.sleep(0.05)
            await qmp.call("migrate-set-capabilities", capabilities=db.X_IGNORE_SHARED)
            await db.migrate(qmp, snap / "state.bin")
        finally:
            await db.stop(vm, qmp)
    log(event="split", seconds=round(time.monotonic() - t0, 1))


async def start(snap: Path, mode: str) -> None:
    run = snap / f"run-{mode}"
    if await anyio.Path(run).exists():
        await anyio.run_process(["rm", "-rf", str(run)])
    t0 = time.monotonic()
    await fresh_overlay(run, snap / "overlay.qcow2")
    await anyio.run_process(["cp", "-c", str(snap / "aux.img"), str(run / "aux.img")])
    if mode == "lazy":
        await anyio.run_process(["cp", "-c", str(snap / "ram.bin"), str(run / "ram.bin")])
        ram = f"memory-backend-file,mem-path={run / 'ram.bin'},share=on"
        state, caps = snap / "state.bin", db.X_IGNORE_SHARED
    else:
        ram, state, caps = "memory-backend-shm", snap / "full.bin", None
    prepared = time.monotonic() - t0
    async with server(run) as sock:
        vm = await db.launch(run, args(run, ram, sock, incoming=True))
        qmp = None
        try:
            qmp = await db.Qmp.connect(run / "qmp.sock")
            if caps:
                await qmp.call("migrate-set-capabilities", capabilities=caps)
            if mode == "eager" and await anyio.Path(snap / "mapped").exists():
                await mapped_ram(qmp, int(await anyio.Path(snap / "mapped").read_text()))
            await qmp.call("migrate-incoming", uri=f"file:{state}")
            while await qmp.status() == "inmigrate":
                await anyio.sleep(0.01)
            loaded = time.monotonic() - t0
            await qmp.call("cont")
            with anyio.fail_after(60):
                while not await db.daemon_answers(run / "vsock.sock"):
                    await anyio.sleep(0.02)
            ready = time.monotonic() - t0
            await db.set_clock(run / "vsock.sock")
            log(event="ready", mode=mode, prepared_s=round(prepared, 3), loaded_s=round(loaded, 3),
                daemon_s=round(ready, 3))  # fmt: skip
            script = run / "bench.sh"
            await anyio.Path(script).write_text(BENCH)
            await db.ssh(run, SSH_PORT, "--", str(script), str(HERE / "bench.c"), "/tmp/")
            await db.ssh(run, SSH_PORT, "sudo -n sh /tmp/bench.sh")
            log(event="benched", mode=mode, total_s=round(time.monotonic() - t0, 1))
        finally:
            await db.stop(vm, qmp)


def main() -> None:
    mode, snap = sys.argv[1], Path(sys.argv[2]).resolve()
    if mode == "save":
        anyio.run(save, snap)
    else:
        anyio.run(start, snap, mode)


if __name__ == "__main__":
    main()
