#!/usr/bin/env agent-python
"""A macOS builder VM per build, started from a snapshot.

    darwin_builder.py prepare IMG_DIR GUEST_DIR   make a builder guest from a tart image
    darwin_builder.py save GUEST_DIR SNAP_DIR     boot GUEST_DIR, wait for its daemon, save
    darwin_builder.py compact IMG_DIR ROOT        IMG_DIR/disk.raw to qcow2, ROOT/*/overlay.qcow2 onto it
    darwin_builder.py serve SNAP_DIR RUN_DIR      start from SNAP_DIR; RUN_DIR/daemon.sock until ^C
    darwin_builder.py build SNAP_DIR RUN_DIR -- PYNIX_BUILD_ARGS...

GUEST_DIR is a builder guest: overlay.qcow2 over IMG_DIR/disk.raw, aux.img,
and in the guest Nix and two LaunchDaemons: nix-daemon --stdio on vsock port
3030, the clock on 3031. `prepare` makes one over SSH (tart's admin/admin,
sudo without a password). IMG_DIR is also the IMG environment variable of the
other steps, because the overlays name its disk.raw.

SNAP_DIR holds the disk overlay as it was at the save (read only from then on),
aux.img, ram.bin (guest RAM) and state.bin (device state; x-ignore-shared
leaves RAM out of it). The saved guest runs on anonymous RAM; a paused QEMU
then moves its RAM into ram.bin. A start maps ram.bin private and
copy-on-write, so pages fault in on first touch and the file never changes, and
puts a fresh qcow2 overlay over the saved one. Every start is the saved guest.

With AGENT (vivarium-runner built for aarch64-darwin), `prepare` also installs
vivarium's guest agent, serving arpyc on /dev/cu.vivarium-agent.

`save` copies the closures of the store paths in SEED (space-separated) from
the host store into the guest first, then builds each Nix file in WARMUP in
the guest. Last, unless ZEROFREE=0, it builds guest/zero-free.nix, which
zeroes the guest's free pages so ram.bin has holes there (ZEROFREE_MIB).

`build` runs `pynix build` against the guest's daemon and copies the outputs
into the host store, then stops the guest. Timings go to stderr as JSON.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import plistlib
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from anyio.abc import ByteStream, Process, SocketStream

HERE = Path(__file__).resolve().parent
QEMU = Path(
    os.environ.get("QEMU", Path.home() / "Dynamist/qemu/build-hvf/qemu-system-aarch64")
)
QEMU_IMG = Path(
    os.environ.get("QEMU_IMG", Path.home() / "Dynamist/qemu/build-hvf/qemu-img")
)
# The host's own booter runs unpatched under HVF.
ROM = Path(
    os.environ.get(
        "ROM",
        "/System/Library/Frameworks/Virtualization.framework/Resources/AVPBooter.vmapple2.bin",
    )
)
RAM = os.environ.get("RAM", "8G")
CPUS = os.environ.get("CPUS", "4")
# The image of a guest that `prepare` did not make: its overlay names this disk.raw.
IMG = Path(os.environ.get("IMG", Path.home() / "Dynamist/orchard-images/sonoma"))
DAEMON_PORT = 3030
HANDSHAKE_TIMEOUT = 2.0
CLOCK_PORT = 3031
STORE_PORT = 3032
VFS_TAG = "hoststore"
VFSD = os.environ.get("VFSD", "darwin-virtiofsd")
VIRTIOFS = os.environ.get("VIRTIOFS") == "1"
# Measured: 8 channels load a 2 GB mapped-ram file in 0.3 s, 4 in 0.26 s but
# the daemon answered 0.25 s later.
MULTIFD_CHANNELS = 8
WORD = struct.Struct("<Q")
WORKER_MAGIC_1 = 0x6E697863
WORKER_MAGIC_2 = 0x6478696F
X_IGNORE_SHARED = [{"capability": "x-ignore-shared", "state": True}]


def log(**fields: object) -> None:
    print(json.dumps(fields), file=sys.stderr, flush=True)


class Qmp:
    def __init__(self, stream: SocketStream) -> None:
        self.stream = stream
        self.buffer = b""

    @classmethod
    async def connect(cls, path: Path) -> Qmp:
        with anyio.fail_after(10):
            while True:
                try:
                    qmp = cls(await anyio.connect_unix(path))
                    break
                except OSError:
                    await anyio.sleep(0.02)
            await qmp.line()
            await qmp.call("qmp_capabilities")
        return qmp

    async def line(self) -> dict[str, object]:
        while b"\n" not in self.buffer:
            self.buffer += await self.stream.receive()
        raw, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(raw)

    async def call(self, execute: str, **arguments: object) -> object:
        message: dict[str, object] = {"execute": execute}
        if arguments:
            message["arguments"] = arguments
        await self.stream.send(json.dumps(message).encode() + b"\n")
        while True:
            reply = await self.line()
            if "error" in reply:
                msg = f"{execute}: {reply['error']}"
                raise RuntimeError(msg)
            if "return" in reply:
                return reply["return"]

    async def status(self) -> str:
        reply = await self.call("query-status")
        if not isinstance(reply, dict):
            raise TypeError(reply)
        return str(reply["status"])


async def connect_guest(vsock: Path, port: int) -> SocketStream:
    """A stream to the guest's *port*, through virtio-vsock-unix's handshake."""
    stream = await anyio.connect_unix(vsock)
    line = b""
    try:
        # A guest whose vsock driver ignores the request never answers at all.
        # fail_after raises TimeoutError, an OSError like a refusal.
        with anyio.fail_after(HANDSHAKE_TIMEOUT):
            await stream.send(f"CONNECT {port}\n".encode())
            while not line.endswith(b"\n"):
                line += await stream.receive(1)
    except BaseException:
        await stream.aclose()
        raise
    if not line.startswith(b"OK "):
        await stream.aclose()
        msg = f"guest port {port} refused: {line!r}"
        raise OSError(msg)
    return stream


async def daemon_answers(vsock: Path) -> bool:
    try:
        async with await connect_guest(vsock, DAEMON_PORT) as stream:
            await stream.send(WORD.pack(WORKER_MAGIC_1))
            reply = b""
            with anyio.move_on_after(2):
                while len(reply) < WORD.size:
                    reply += await stream.receive(WORD.size - len(reply))
    except OSError, anyio.EndOfStream, anyio.BrokenResourceError:
        return False
    return len(reply) == WORD.size and WORD.unpack(reply)[0] == WORKER_MAGIC_2


async def set_clock(vsock: Path) -> str:
    """Set the guest clock; returns en0's address, which the guest renews if it has none."""
    async with await connect_guest(vsock, CLOCK_PORT) as stream:
        await stream.send(f"{int(time.time())}\n".encode())
        await stream.send_eof()
        reply = b""
        with contextlib.suppress(anyio.EndOfStream):
            while True:
                reply += await stream.receive()
    words = reply.split()
    if not words or words[0] != b"ok":
        msg = f"the guest did not set its clock: {reply!r}"
        raise RuntimeError(msg)
    return words[1].decode() if len(words) > 1 else ""


async def pump(source: ByteStream, sink: ByteStream) -> None:
    with contextlib.suppress(
        anyio.BrokenResourceError, anyio.ClosedResourceError, anyio.EndOfStream
    ):
        async for chunk in source:
            await sink.send(chunk)
    # Half-close, so the far side sees EOF and still answers.
    with contextlib.suppress(
        anyio.BrokenResourceError, anyio.ClosedResourceError, OSError
    ):
        await sink.send_eof()


async def forward(
    vsock: Path, listen: Path, *, task_status: anyio.abc.TaskStatus[None]
) -> None:
    """Serve the guest daemon on *listen*, a plain Unix socket Nix can use."""
    await anyio.Path(listen).unlink(missing_ok=True)
    listener = await anyio.create_unix_listener(listen)

    async def handle(client: SocketStream) -> None:
        async with client:
            try:
                guest = await connect_guest(vsock, DAEMON_PORT)
            # A reset during the handshake is BrokenResourceError, not an OSError.
            except (OSError, anyio.EndOfStream, anyio.BrokenResourceError) as exc:
                log(event="refused", error=str(exc) or type(exc).__name__)
                return
            async with guest, anyio.create_task_group() as tg:
                tg.start_soon(pump, client, guest)
                tg.start_soon(pump, guest, client)

    task_status.started()
    await listener.serve(handle)


def ecid(img: Path) -> int:
    config = json.loads((img / "config.json").read_text())
    return int(plistlib.loads(base64.b64decode(config["ecid"]))["ECID"])


def base_disk(img: Path) -> tuple[Path, str]:
    """IMG_DIR's disk and its format: disk.qcow2 once `compact` made it, else tart's disk.raw."""
    qcow2 = img / "disk.qcow2"
    return (qcow2, "qcow2") if qcow2.exists() else (img / "disk.raw", "raw")


def qemu_args(
    img: Path, run: str, ram: str, *, ssh_port: int | None = None
) -> list[str]:
    """The arguments of run-hvf.sh, with the guest's RAM the memory backend *ram*.

    *run* is the run directory, or ``{run}`` for a snapshot's template.
    """
    netdev = "user,id=net0" + (
        f",hostfwd=tcp:127.0.0.1:{ssh_port}-:22" if ssh_port else ""
    )
    disk, disk_format = base_disk(img)
    return [
        "-M", f"vmapple,uuid={ecid(img)},gfx=off,memory-backend=ram0",
        "-accel", "hvf",
        "-m", RAM, "-smp", CPUS,
        "-object", f"{ram},id=ram0,size={RAM}",
        "-bios", str(ROM),
        "-drive", f"file={run}/aux.img,if=pflash,format=raw",
        "-drive", f"file={disk},if=pflash,format={disk_format},readonly=on",
        "-drive", f"file={run}/aux.img,if=none,id=aux,format=raw",
        "-device", "vmapple-virtio-blk-pci,variant=aux,drive=aux,share-rw=on",
        "-drive", f"file={run}/overlay.qcow2,if=none,id=root,format=qcow2,cache=writeback,discard=unmap",
        "-device", "vmapple-virtio-blk-pci,variant=root,drive=root",
        "-netdev", netdev,
        "-device", "virtio-net-pci,netdev=net0",
        "-chardev", f"file,id=ser0,path={run}/serial.txt",
        "-serial", "chardev:ser0",
        "-qmp", f"unix:{run}/qmp.sock,server=on,wait=off",
        "-device", "virtio-serial-pci,id=vser0",
        "-chardev", f"socket,id=agent,path={run}/agent.sock,server=on,wait=off",
        "-device", "virtserialport,bus=vser0.0,chardev=agent,name=vivarium-agent",
        "-device", f"virtio-vsock-unix-pci,guest-cid=3,path={run}/vsock.sock",
        "-display", "none",
    ]  # fmt: skip


def vfs_args(run: str) -> list[str]:
    """The vhost-user-fs device; guest RAM must then be memory-backend-shm."""
    return [
        "-chardev", f"socket,id=vfs,path={run}/vfs.sock",
        "-device", f"vhost-user-fs-pci,chardev=vfs,tag={VFS_TAG}",
    ]  # fmt: skip


@contextlib.asynccontextmanager
async def vfs_server(run: Path) -> AsyncIterator[None]:
    """darwin-virtiofsd serving the host store at RUN/vfs.sock, for one QEMU."""
    sock = run / "vfs.sock"
    await anyio.Path(sock).unlink(missing_ok=True)
    out = await anyio.open_file(run / "vfsd.log", "ab")
    async with await anyio.open_process(
        [VFSD, "--socket-path", str(sock), "--shared-dir", "/nix/store"],
        stdout=out.wrapped, stderr=out.wrapped,
    ) as proc:  # fmt: skip
        with anyio.fail_after(5):
            while not await anyio.Path(sock).exists():
                await anyio.sleep(0.005)
        try:
            yield
        finally:
            with anyio.CancelScope(shield=True):
                if proc.returncode is None:
                    proc.terminate()
                await proc.wait()


async def mapped_ram(qmp: Qmp, channels: int) -> None:
    """RAM at fixed offsets, zero pages left out, moved by CHANNELS threads."""
    await qmp.call("migrate-set-capabilities", capabilities=[
        {"capability": "mapped-ram", "state": True},
        {"capability": "multifd", "state": True},
    ])  # fmt: skip
    await qmp.call("migrate-set-parameters", **{"multifd-channels": channels})


async def store_command(vsock: Path, command: str, data: bytes = b"") -> str:
    """One command to guest/builder-store.sh; returns its output."""
    async with await connect_guest(vsock, STORE_PORT) as stream:
        await stream.send(f"{command}\n".encode() + data)
        await stream.send_eof()
        reply = b""
        with contextlib.suppress(anyio.EndOfStream):
            while True:
                reply += await stream.receive()
    text = reply.decode(errors="replace").strip()
    if not text.endswith("ok"):
        msg = f"builder-store {command}: {text}"
        raise RuntimeError(msg)
    return text


async def launch(run: Path, args: list[str], qemu: Path = QEMU) -> Process:
    out = await anyio.open_file(run / "stdout.log", "ab")
    return await anyio.open_process(
        [str(qemu), *args], stdout=out.wrapped, stderr=out.wrapped
    )


async def stop(vm: Process, qmp: Qmp | None) -> None:
    with anyio.CancelScope(shield=True):
        if vm.returncode is None and qmp is not None:
            with contextlib.suppress(Exception):
                await qmp.call("quit")
        with anyio.move_on_after(5):
            await vm.wait()
            return
        vm.kill()
        await vm.wait()


async def migrate(qmp: Qmp, target: Path) -> None:
    await qmp.call("migrate", uri=f"file:{target}")
    while True:
        status = await qmp.call("query-migrate")
        if isinstance(status, dict) and status.get("status") in (
            "completed",
            "failed",
            "cancelled",
        ):
            break
        await anyio.sleep(0.05)
    if status.get("status") != "completed":
        msg = f"migration to {target} failed: {status}"
        raise RuntimeError(msg)


async def split_ram(img: Path, snap: Path) -> None:
    """Turn full.bin, RAM and all, into ram.bin and state.bin.

    A paused QEMU loads full.bin into a shared RAM file, then saves its device
    state with x-ignore-shared. No guest ever runs on the shared mapping.
    """
    t0 = time.monotonic()
    ram = f"memory-backend-file,mem-path={snap / 'ram.bin'},share=on"
    vm = await launch(snap, [*qemu_args(img, str(snap), ram), "-incoming", "defer"])
    qmp = None
    try:
        qmp = await Qmp.connect(snap / "qmp.sock")
        await qmp.call("migrate-incoming", uri=f"file:{snap / 'full.bin'}")
        while await qmp.status() == "inmigrate":
            await anyio.sleep(0.05)
        await qmp.call("migrate-set-capabilities", capabilities=X_IGNORE_SHARED)
        await migrate(qmp, snap / "state.bin")
    finally:
        await stop(vm, qmp)
    await anyio.Path(snap / "full.bin").unlink()
    log(event="split", seconds=round(time.monotonic() - t0, 1))


async def save(guest: Path, snap: Path) -> None:
    image = anyio.Path(guest / "image")
    img = Path((await image.read_text()).strip()) if await image.exists() else IMG
    await anyio.Path(snap).mkdir(parents=True)
    for name in ("overlay.qcow2", "aux.img"):
        await anyio.run_process(["cp", "-c", str(guest / name), str(snap / name)])
    async with vfs_server(snap) if VIRTIOFS else contextlib.nullcontext():
        await save_running(img, snap)
    if VIRTIOFS:
        # state.bin holds the RAM too; nothing to split.
        await anyio.Path(snap / "overlay.qcow2").chmod(0o444)
        args = [*qemu_args(img, "{run}", "memory-backend-shm"), *vfs_args("{run}")]
        server = shutil.which(VFSD) or VFSD
        virtiofs = {"tag": VFS_TAG, "storeVsockPort": STORE_PORT,
                    "server": str(await anyio.Path(server).resolve())}  # fmt: skip
        await write_manifest(img, snap, args, mappedRam=MULTIFD_CHANNELS, virtiofs=virtiofs)
        return
    await split_ram(img, snap)
    await anyio.Path(snap / "overlay.qcow2").chmod(0o444)
    # Private: with a MAP_SHARED mapping the host pager writes dirty guest pages
    # back to the file, and the guest stalled for 5-25 s at a time. The state
    # carries no RAM, so the destination keeps the file's pages.
    ram = (
        "memory-backend-file,mem-path={snapshot}/ram.bin,share=off,readonly=on,rom=off"
    )
    await write_manifest(img, snap, qemu_args(img, "{run}", ram))


async def write_manifest(
    img: Path, snap: Path, args: list[str], **extra: object
) -> None:
    # Paths inside the snapshot are relative, and {snapshot} in the arguments,
    # so the directory can move.
    manifest = {
        "qemu": str(await anyio.Path(QEMU).resolve()),
        "qemuImg": str(await anyio.Path(QEMU_IMG).resolve()),
        "args": [*args, "-incoming", "defer"],
        "overlay": "overlay.qcow2",
        "aux": "aux.img",
        "state": "state.bin",
        "daemonVsockPort": DAEMON_PORT,
        "clockVsockPort": CLOCK_PORT,
        **extra,
    }
    await anyio.Path(snap / "snapshot.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "snapshot": str(snap),
                "state_bytes": (await anyio.Path(snap / "state.bin").stat()).st_size,
            }
        )
    )


async def save_running(img: Path, snap: Path) -> None:
    """Boot the guest, prepare it and migrate it to SNAP/full.bin (state.bin with VIRTIOFS)."""
    if VIRTIOFS:
        args = [*qemu_args(img, str(snap), "memory-backend-shm"), *vfs_args(str(snap))]
    else:
        # Anonymous RAM: a guest running on a shared file mapping stalls for
        # seconds at a time while the host pager writes its pages back.
        args = qemu_args(img, str(snap), "memory-backend-ram")
    vm = await launch(snap, args)
    qmp = None
    try:
        qmp = await Qmp.connect(snap / "qmp.sock")
        start = time.monotonic()
        with anyio.fail_after(300):
            while not await daemon_answers(snap / "vsock.sock"):
                if vm.returncode is not None:
                    sys.exit(f"QEMU exited with {vm.returncode}; see {snap}/stdout.log")
                await anyio.sleep(0.5)
        log(event="daemon", seconds=round(time.monotonic() - start, 1))
        store = f"unix://{snap / 'daemon.sock'}?local-fs=false"
        async with anyio.create_task_group() as tg:
            await tg.start(forward, snap / "vsock.sock", snap / "daemon.sock")
            if seed := os.environ.get("SEED", "").split():
                # Each start would otherwise substitute these again.
                await anyio.run_process(
                    ["pynix", "copy", "--no-check-sigs", "--to", store, *seed],
                    stdout=None, stderr=None,
                )  # fmt: skip
                log(
                    event="seeded",
                    paths=len(seed),
                    seconds=round(time.monotonic() - start, 1),
                )
            # A build before the save leaves its .drv closure in the guest store
            # and its tools in the guest's file cache, so a start skips both.
            for expr in os.environ.get("WARMUP", "").split():
                await anyio.run_process(
                    ["pynix", "build", "--store", store, "--eval-store", "auto", "--file", expr],
                    stdout=subprocess.DEVNULL, stderr=None,
                )  # fmt: skip
                log(
                    event="warmed",
                    file=expr,
                    seconds=round(time.monotonic() - start, 1),
                )
            # Let launchd settle, so the snapshot is of an idle guest.
            await anyio.sleep(float(os.environ.get("SETTLE", "30")))
            if VIRTIOFS:
                # A start puts the host store under the guest's store volume,
                # so that volume must be up, and the share unmounted.
                status = await store_command(snap / "vsock.sock", "status")
                if " union" not in status or "virtio-fs" in status:
                    sys.exit(f"the guest's store is not one union volume: {status}")
                await store_command(snap / "vsock.sock", "zerofree")
                log(event="zeroed")
            elif os.environ.get("ZEROFREE", "1") != "0":
                built = await anyio.run_process(
                    ["pynix", "build", "--store", store, "--eval-store", "auto", "--copy", "auto",
                     "--file", str(HERE.parent / "guest/zero-free.nix")],
                    stderr=None,
                )  # fmt: skip
                out = json.loads(built.stdout)["outputs"]["out"]
                log(event="zeroed", report=(await anyio.Path(out).read_text()).strip())
            tg.cancel_scope.cancel()
        # A macOS 15 guest was once saved with no address on en0: every start
        # of that snapshot had no network. The clock port renews the lease.
        address = await set_clock(snap / "vsock.sock")
        if not address:
            sys.exit(
                "the guest has no address on en0 after a DHCP renewal; not saving it"
            )
        log(event="network", address=address)
        await qmp.call("stop")
        if VIRTIOFS:
            # Guest RAM is shared memory, which x-ignore-shared would leave out:
            # the whole RAM goes into state.bin, as mapped-ram.
            await mapped_ram(qmp, MULTIFD_CHANNELS)
            await migrate(qmp, snap / "state.bin")
        else:
            await migrate(qmp, snap / "full.bin")
        log(event="saved", seconds=round(time.monotonic() - start, 1))
    finally:
        await stop(vm, qmp)


async def ssh(
    run: Path, port: int, *command: str, check: bool = True, connect_timeout: int = 10
) -> bool:
    """Run *command* in the guest as admin; with a "--" upload, ``scp`` instead."""
    askpass = anyio.Path(run / "askpass")
    if not await askpass.exists():
        await askpass.write_text("#!/bin/sh\necho admin\n")
        await askpass.chmod(0o700)
    env = os.environ | {
        "SSH_ASKPASS": str(askpass),
        "SSH_ASKPASS_REQUIRE": "force",
        "DISPLAY": ":0",
    }
    options = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
               "-o", f"ConnectTimeout={connect_timeout}"]  # fmt: skip
    if command[0] == "--":
        *sources, target = command[1:]
        argv = ["scp", "-P", str(port), *options, *sources, f"admin@127.0.0.1:{target}"]
    else:
        argv = ["ssh", "-p", str(port), *options, "admin@127.0.0.1", *command]
    result = await anyio.run_process(
        argv,
        env=env,
        check=False,
        stdout=None if check else subprocess.DEVNULL,
        # The guest's errors belong in this log; a probe's are noise.
        stderr=None if check else subprocess.DEVNULL,
    )
    if check and result.returncode != 0:
        msg = f"{argv[0]} {' '.join(command)}: exit {result.returncode}"
        raise RuntimeError(msg)
    return result.returncode == 0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def reload_daemon(label: str, plist: str) -> str:
    """Shell that (re)loads org.nixidae.*label* from /Library/LaunchDaemons/*plist*.plist.

    Not `kickstart -k`: that restarts the loaded definition, so a changed
    plist never takes effect. bootout returns before the job is gone, so
    bootstrap is retried.
    """
    return (
        f"launchctl bootout system/org.nixidae.{label} 2>/dev/null; "
        f"for i in $(seq 20); do launchctl bootstrap system /Library/LaunchDaemons/{plist}.plist "
        "2>/dev/null && break; sleep 0.5; done; "
        f"launchctl print system/org.nixidae.{label} >/dev/null"
    )


async def install_agent(guest: Path, port: int, agent: str) -> None:
    """vivarium's guest agent from the host store, as a LaunchDaemon on the virtio-serial port."""
    async with anyio.create_task_group() as tg:
        await tg.start(forward, guest / "vsock.sock", guest / "daemon.sock")
        await anyio.run_process(
            ["pynix", "copy", "--no-check-sigs", "--to", f"unix://{guest / 'daemon.sock'}?local-fs=false", agent],
            stdout=subprocess.DEVNULL, stderr=None,
        )  # fmt: skip
        tg.cancel_scope.cancel()
    await ssh(
        guest, port, "--", str(HERE.parent / "guest/vivarium-agent.plist"), "/tmp/"
    )
    # The profile roots the closure, so the guest's garbage collector keeps it.
    await ssh(guest, port, "sudo -n sh -c '/nix/var/nix/profiles/default/bin/nix-env "
              f"-p /nix/var/nix/profiles/vivarium-agent --set {agent} && "
              "install -m 644 /tmp/vivarium-agent.plist /Library/LaunchDaemons/ && "
              f"{reload_daemon('vivarium-agent', 'vivarium-agent')}'")  # fmt: skip
    log(event="agent", path=agent)


async def install_store(guest: Path, port: int) -> None:
    """The guest's store onto its own volume, union-mounted, and guest/builder-store.sh.

    A union mount reaches below only at its root, so the store paths must be
    the volume's root. The installer's nix-daemon would hold that volume busy;
    the builder's daemon is vsock's `nix-daemon --stdio`, so it goes.
    """
    zerofree = guest / "zerofree"
    await anyio.run_process(["/usr/bin/clang", "-O2", "-mmacosx-version-min=14.0", "-o", str(zerofree),
                             str(HERE.parent / "guest/zerofree.c")])  # fmt: skip
    files = [zerofree, *(HERE.parent / "guest" / name for name in
             ("builder-store.sh", "builder-store.plist", "builder-store-boot.plist"))]  # fmt: skip
    await ssh(guest, port, "--", *map(str, files), "/tmp/")
    script = (
        "set -ex; "
        "install -m 755 /tmp/zerofree /usr/local/bin/zerofree; "
        "install -m 755 /tmp/builder-store.sh /usr/local/bin/builder-store; "
        "launchctl bootout system/org.nixos.nix-daemon 2>/dev/null || true; "
        "launchctl disable system/org.nixos.nix-daemon; "
        "if ! diskutil info 'Nix Upper' >/dev/null 2>&1; then "
        "  c=$(diskutil info 'Nix Store' | awk '/APFS Container:/ {print $3}'); "
        "  diskutil apfs addVolume \"$c\" APFS 'Nix Upper' -nomount >/dev/null; "
        "fi; "
        "dev=$(diskutil info 'Nix Upper' | awk '/Device Node:/ {print $3}'); "
        # Else diskarbitration mounts it under /Volumes at boot, as the Nix
        # installer's fstab line stops it doing to the Nix Store volume.
        "uuid=$(diskutil info 'Nix Upper' | awk '/Volume UUID:/ {print $3}'); "
        'grep -q "^UUID=$uuid " /etc/fstab 2>/dev/null || '
        'echo "UUID=$uuid /nix/store apfs rw,noauto,nobrowse" >> /etc/fstab; '
        'if ! mount | grep -q "^$dev on /nix/store "; then '
        '  mkdir -p /tmp/upper; mount -t apfs -o nobrowse "$dev" /tmp/upper; '
        "  touch /tmp/upper/.metadata_never_index; "
        "  chown root:nixbld /tmp/upper; chmod 1775 /tmp/upper; "
        "  find /nix/store -mindepth 1 -maxdepth 1 -exec mv {} /tmp/upper/ \\; ; "
        '  echo "moved $(ls -A /tmp/upper | wc -l) entries, $(ls -A /nix/store | wc -l) left"; '
        "  umount /tmp/upper; /usr/local/bin/builder-store boot; "
        "fi; "
        "install -m 644 /tmp/builder-store.plist /tmp/builder-store-boot.plist /Library/LaunchDaemons/; "
        # reload_daemon's bootout fails on a job that is not loaded yet.
        "set +e; "
        f"for d in store store-boot; do {reload_daemon('builder-$d', 'builder-$d')} || exit 1; done; "
        "mount | grep ' on /nix/store '"
    )
    await ssh(guest, port, f"sudo -n sh -c {shlex.quote(script)}")
    log(event="store", volume="Nix Upper")


async def prepare(img: Path, guest: Path) -> None:
    # Again on a prepared guest: Nix stays, vsockcat and the daemons are replaced.
    await anyio.Path(guest).mkdir(parents=True, exist_ok=True)
    vsockcat = guest / "vsockcat"
    await anyio.run_process(["/usr/bin/clang", "-O2", "-mmacosx-version-min=14.0", "-o", str(vsockcat),
                             str(HERE.parent / "guest/vsockcat.c")])  # fmt: skip
    port = free_port()
    if not await anyio.Path(guest / "overlay.qcow2").exists():
        disk, disk_format = base_disk(img)
        await anyio.run_process([str(QEMU_IMG), "create", "-q", "-f", "qcow2", "-F", disk_format, "-b",
                                 str(disk), str(guest / "overlay.qcow2")])  # fmt: skip
        await anyio.run_process(
            ["cp", "-c", str(img / "aux.img"), str(guest / "aux.img")]
        )
    await anyio.Path(guest / "image").write_text(f"{img}\n")
    # Anonymous: a shared file mapping stalls the guest, and nothing reads this RAM later.
    vm = await launch(
        guest, qemu_args(img, str(guest), "memory-backend-ram", ssh_port=port)
    )
    qmp = None
    try:
        qmp = await Qmp.connect(guest / "qmp.sock")
        start = time.monotonic()
        with anyio.fail_after(300):
            while not await ssh(guest, port, "true", check=False, connect_timeout=5):
                if vm.returncode is not None:
                    sys.exit(
                        f"QEMU exited with {vm.returncode}; see {guest}/stdout.log"
                    )
                await anyio.sleep(1)
        log(event="ssh", seconds=round(time.monotonic() - start, 1))
        # tart's daemon reboots the guest when tart's host side is absent.
        await ssh(guest, port, "sudo -n launchctl disable system/org.cirruslabs.tart-guest-daemon; "
                  "sudo -n launchctl bootout system/org.cirruslabs.tart-guest-daemon || true")  # fmt: skip
        # The Nix Store volume mounts up to 30 s after sshd starts. An install
        # over an unmounted volume fails, and its cleanup removed
        # /etc/synthetic.conf, which breaks /nix for good.
        await ssh(guest, port, "if diskutil info 'Nix Store' >/dev/null 2>&1; then "
                  "for i in $(seq 90); do test -d /nix/store && exit 0; sleep 1; done; "
                  "echo 'the Nix Store volume exists and never mounted at /nix'; exit 1; fi")  # fmt: skip
        await ssh(guest, port, "test -e /nix/var/nix/profiles/default/bin/nix-daemon || "
                  "{ curl -sSfL https://nixos.org/nix/install -o /tmp/install-nix && "
                  "sh /tmp/install-nix --daemon --yes > /tmp/install-nix.log 2>&1 || "
                  "{ tail -20 /tmp/install-nix.log; exit 1; }; }")  # fmt: skip
        log(event="nix", seconds=round(time.monotonic() - start, 1))
        plists = [
            HERE.parent / "guest" / name
            for name in ("builder-daemon.plist", "builder-clock.plist")
        ]
        await ssh(guest, port, "--", str(vsockcat), *map(str, plists), "/tmp/")
        await ssh(guest, port, "sudo -n sh -c 'install -m 755 /tmp/vsockcat /usr/local/bin/vsockcat && "
                  "install -m 644 /tmp/builder-daemon.plist /tmp/builder-clock.plist /Library/LaunchDaemons/ && "
                  f"for d in daemon clock; do {reload_daemon('builder-$d', 'builder-$d')} || exit 1; done'")  # fmt: skip
        with anyio.move_on_after(60) as waited:
            while not await daemon_answers(guest / "vsock.sock"):
                await anyio.sleep(0.5)
        if waited.cancelled_caught:
            await ssh(guest, port, "sudo -n launchctl print system/org.nixidae.builder-daemon | "
                      "grep -E 'state|exit'; sudo -n tail -5 /var/log/builder-daemon.log", check=False)  # fmt: skip
            sys.exit(
                f"the guest daemon did not answer over vsock within 60 s; see {guest}/stdout.log"
            )
        await install_store(guest, port)
        if agent := os.environ.get("AGENT"):
            await install_agent(guest, port, agent)
        await ssh(guest, port, "sudo -n shutdown -h now", check=False)
        with anyio.fail_after(60):
            await vm.wait()
        log(event="prepared", seconds=round(time.monotonic() - start, 1))
    finally:
        await stop(vm, qmp)


async def compact(img: Path, root: Path) -> None:
    """IMG_DIR/disk.raw to disk.qcow2, and every overlay under *root* onto it.

    COMPRESS: zstd (default), zlib or none. `qemu-img compare` proves the
    content identical, so `rebase -u` is safe and a saved RAM still matches
    its disk. disk.raw stays until the caller removes it.
    """
    raw, qcow2 = img / "disk.raw", img / "disk.qcow2"
    compression = os.environ.get("COMPRESS", "zstd")
    t0 = time.monotonic()
    if not await anyio.Path(qcow2).exists():
        partial = img / "disk.qcow2.partial"
        options = (
            ["-c", "-o", f"compression_type={compression}"]
            if compression != "none"
            else []
        )
        await anyio.run_process(
            [str(QEMU_IMG), "convert", "-O", "qcow2", *options, str(raw), str(partial)]
        )
        await anyio.run_process(
            [
                str(QEMU_IMG),
                "compare",
                "-q",
                "-f",
                "raw",
                "-F",
                "qcow2",
                str(raw),
                str(partial),
            ]
        )
        await anyio.Path(partial).chmod(0o444)
        await anyio.Path(partial).rename(qcow2)
        log(event="converted", seconds=round(time.monotonic() - t0, 1))
    async for overlay in anyio.Path(root).glob("*/overlay.qcow2"):
        info = json.loads(
            (
                await anyio.run_process(
                    [str(QEMU_IMG), "info", "--output=json", "-U", str(overlay)]
                )
            ).stdout
        )
        if info.get("backing-filename") != str(raw):
            continue
        mode = (await overlay.stat()).st_mode
        await overlay.chmod(mode | 0o200)
        try:
            await anyio.run_process(
                [
                    str(QEMU_IMG),
                    "rebase",
                    "-u",
                    "-F",
                    "qcow2",
                    "-b",
                    str(qcow2),
                    str(overlay),
                ]
            )
        finally:
            await overlay.chmod(mode)
        manifest = anyio.Path(overlay.parent / "snapshot.json")
        if await manifest.exists():
            text = await manifest.read_text()
            await manifest.write_text(
                text.replace(
                    f"file={raw},if=pflash,format=raw",
                    f"file={qcow2},if=pflash,format=qcow2",
                )
            )
        log(event="rebased", overlay=str(overlay))
    log(event="compacted", seconds=round(time.monotonic() - t0, 1))


@dataclass(frozen=True)
class Started:
    socket: Path
    #: QEMU takes one QMP client at a time, and this is it.
    qmp: Qmp
    vsock: Path
    #: The guest's store lies over the host's: a path the host has needs only registering.
    host_store: bool


@contextlib.asynccontextmanager
async def started(snap: Path, run: Path) -> AsyncIterator[Started]:
    """A guest from *snap*, with its daemon served on a Unix socket."""
    if await anyio.Path(run).exists():
        await anyio.to_thread.run_sync(shutil.rmtree, run)
    await anyio.Path(run).mkdir(parents=True)
    t0 = time.monotonic()
    manifest = json.loads(await anyio.Path(snap / "snapshot.json").read_text())

    def at(name: str) -> str:
        return name if Path(name).is_absolute() else str(snap / name)

    await anyio.run_process(["cp", "-c", at(manifest["aux"]), str(run / "aux.img")])
    await anyio.run_process(
        [manifest["qemuImg"], "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", at(manifest["overlay"]),
         str(run / "overlay.qcow2")],
    )  # fmt: skip
    args = [
        arg.replace("{run}", str(run)).replace("{snapshot}", str(snap))
        for arg in manifest["args"]
    ]
    host_store = "virtiofs" in manifest
    async with contextlib.AsyncExitStack() as stack:
        if host_store:
            await stack.enter_async_context(vfs_server(run))
        vm = await launch(run, args, Path(os.environ.get("QEMU", manifest["qemu"])))
        qmp: Qmp | None = None
        # Runs before the server stops: QEMU goes first, then its back-end.
        stack.push_async_callback(lambda: stop(vm, qmp))
        qmp = await Qmp.connect(run / "qmp.sock")
        if channels := manifest.get("mappedRam"):
            await mapped_ram(qmp, channels)
        else:
            await qmp.call("migrate-set-capabilities", capabilities=X_IGNORE_SHARED)
        await qmp.call("migrate-incoming", uri=f"file:{at(manifest['state'])}")
        while await qmp.status() == "inmigrate":
            await anyio.sleep(0.01)
        loaded = time.monotonic() - t0
        await qmp.call("cont")
        vsock = run / "vsock.sock"
        with anyio.fail_after(60):
            while not await daemon_answers(vsock):
                if vm.returncode is not None:
                    sys.exit(f"QEMU exited with {vm.returncode}; see {run}/stdout.log")
                await anyio.sleep(0.05)
        ready = time.monotonic() - t0
        await set_clock(vsock)
        clocked = time.monotonic() - t0
        if host_store:
            await store_command(vsock, "host")
        log(event="ready", loaded_s=round(loaded, 3), daemon_s=round(ready, 3),
            clock_s=round(clocked, 3), store_s=round(time.monotonic() - t0, 3))  # fmt: skip
        async with anyio.create_task_group() as tg:
            await tg.start(forward, vsock, run / "daemon.sock")
            yield Started(run / "daemon.sock", qmp, vsock, host_store)
            tg.cancel_scope.cancel()


async def serve(snap: Path, run: Path) -> None:
    async with started(snap, run) as guest:
        print(f"unix://{guest.socket}?local-fs=false", flush=True)
        await anyio.sleep_forever()


async def drv_path(args: list[str]) -> str | None:
    """The derivation `pynix build ARGS` builds, for --file F [--attr A]; else None."""
    file = attr = None
    it = iter(args)
    for arg in it:
        if arg in ("--file", "-f"):
            file = next(it, None)
        elif arg in ("--attr", "-A"):
            attr = next(it, None)
        elif arg.startswith("-"):
            return None
    if file is None:
        return None
    evaluated = await anyio.run_process(
        [
            "pynix",
            "eval",
            "--file",
            file,
            "--attr",
            f"{attr}.drvPath" if attr else "drvPath",
        ]
    )
    return str(json.loads(evaluated.stdout))


async def register_closure(vsock: Path, drv: str) -> int:
    """Register DRV's closure, with the outputs the host has, in the guest's store.

    The guest reads the paths over virtiofs, so it needs their database rows
    and nothing else. pynix has no --include-outputs query or --dump-db.
    """
    closure = await anyio.run_process(
        ["nix-store", "--query", "--requisites", "--include-outputs", drv]
    )
    paths = closure.stdout.decode().split()
    rows = await anyio.run_process(["nix-store", "--dump-db", *paths])
    await store_command(vsock, "load-db", rows.stdout)
    return len(paths)


async def build(snap: Path, run: Path, args: list[str]) -> int:
    t0 = time.monotonic()
    async with started(snap, run) as guest:
        if guest.host_store and (drv := await drv_path(args)):
            count = await register_closure(guest.vsock, drv)
            log(
                event="registered", paths=count, seconds=round(time.monotonic() - t0, 3)
            )
        command = ["pynix", "build", "--store", f"unix://{guest.socket}?local-fs=false", "--eval-store", "auto",
                   "--copy", "auto", *args]  # fmt: skip
        result = await anyio.run_process(command, stdout=None, stderr=None, check=False)
        built = time.monotonic() - t0
    log(
        event="done",
        build_s=round(built, 3),
        total_s=round(time.monotonic() - t0, 3),
        status=result.returncode,
    )
    return result.returncode


def main() -> None:
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    mode, a, b = sys.argv[1], Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve()
    if mode == "prepare":
        anyio.run(prepare, a, b)
    elif mode == "save":
        anyio.run(save, a, b)
    elif mode == "compact":
        anyio.run(compact, a, b)
    elif mode == "serve":
        anyio.run(serve, a, b)
    elif mode == "build" and sys.argv[4:5] == ["--"]:
        sys.exit(anyio.run(build, a, b, sys.argv[5:]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
