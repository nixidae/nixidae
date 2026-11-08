#!/usr/bin/env agent-python
"""A macOS builder VM per build, started from a snapshot.

    darwin_builder.py prepare IMG_DIR GUEST_DIR   make a builder guest from a tart image
    darwin_builder.py save GUEST_DIR SNAP_DIR     boot GUEST_DIR, wait for its daemon, save
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
the host store into the guest first.

`build` runs `pynix build` against the guest's daemon and copies the outputs
into the host store, then stops the guest. Timings go to stderr as JSON.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import plistlib
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


def qemu_args(
    img: Path, run: str, ram: str, *, ssh_port: int | None = None
) -> list[str]:
    """The arguments of run-hvf.sh, with the guest's RAM the memory backend *ram*.

    *run* is the run directory, or ``{run}`` for a snapshot's template.
    """
    netdev = "user,id=net0" + (
        f",hostfwd=tcp:127.0.0.1:{ssh_port}-:22" if ssh_port else ""
    )
    return [
        "-M", f"vmapple,uuid={ecid(img)},gfx=off,memory-backend=ram0",
        "-accel", "hvf",
        "-m", RAM, "-smp", CPUS,
        "-object", f"{ram},id=ram0,size={RAM}",
        "-bios", str(ROM),
        "-drive", f"file={run}/aux.img,if=pflash,format=raw",
        "-drive", f"file={img}/disk.raw,if=pflash,format=raw,readonly=on",
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
    # Anonymous RAM: a guest running on a shared file mapping stalls for
    # seconds at a time while the host pager writes its pages back.
    vm = await launch(snap, qemu_args(img, str(snap), "memory-backend-ram"))
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
        if seed := os.environ.get("SEED", "").split():
            # Each start would otherwise substitute these again.
            async with anyio.create_task_group() as tg:
                await tg.start(forward, snap / "vsock.sock", snap / "daemon.sock")
                await anyio.run_process(
                    ["pynix", "copy", "--no-check-sigs", "--to", f"unix://{snap / 'daemon.sock'}?local-fs=false",
                     *seed],
                    stdout=None, stderr=None,
                )  # fmt: skip
                tg.cancel_scope.cancel()
            log(
                event="seeded",
                paths=len(seed),
                seconds=round(time.monotonic() - start, 1),
            )
        # Let launchd settle, so the snapshot is of an idle guest.
        await anyio.sleep(float(os.environ.get("SETTLE", "30")))
        # A macOS 15 guest was once saved with no address on en0: every start
        # of that snapshot had no network. The clock port renews the lease.
        address = await set_clock(snap / "vsock.sock")
        if not address:
            sys.exit(
                "the guest has no address on en0 after a DHCP renewal; not saving it"
            )
        log(event="network", address=address)
        await qmp.call("stop")
        await migrate(qmp, snap / "full.bin")
        log(event="saved", seconds=round(time.monotonic() - start, 1))
    finally:
        await stop(vm, qmp)
    await split_ram(img, snap)
    await anyio.Path(snap / "overlay.qcow2").chmod(0o444)
    # Private: with a MAP_SHARED mapping the host pager writes dirty guest pages
    # back to the file, and the guest stalled for 5-25 s at a time. The state
    # carries no RAM, so the destination keeps the file's pages.
    ram = (
        f"memory-backend-file,mem-path={snap / 'ram.bin'},share=off,readonly=on,rom=off"
    )
    manifest = {
        "qemu": str(await anyio.Path(QEMU).resolve()),
        "qemuImg": str(await anyio.Path(QEMU_IMG).resolve()),
        "args": [*qemu_args(img, "{run}", ram), "-incoming", "defer"],
        "overlay": str(snap / "overlay.qcow2"),
        "aux": str(snap / "aux.img"),
        "state": str(snap / "state.bin"),
        "daemonVsockPort": DAEMON_PORT,
        "clockVsockPort": CLOCK_PORT,
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
        argv, env=env, check=False, stdout=None if check else subprocess.DEVNULL
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


async def prepare(img: Path, guest: Path) -> None:
    # Again on a prepared guest: Nix stays, vsockcat and the daemons are replaced.
    await anyio.Path(guest).mkdir(parents=True, exist_ok=True)
    vsockcat = guest / "vsockcat"
    await anyio.run_process(["/usr/bin/clang", "-O2", "-mmacosx-version-min=14.0", "-o", str(vsockcat),
                             str(HERE.parent / "guest/vsockcat.c")])  # fmt: skip
    port = free_port()
    if not await anyio.Path(guest / "overlay.qcow2").exists():
        await anyio.run_process([str(QEMU_IMG), "create", "-q", "-f", "qcow2", "-F", "raw", "-b",
                                 str(img / "disk.raw"), str(guest / "overlay.qcow2")])  # fmt: skip
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
        if agent := os.environ.get("AGENT"):
            await install_agent(guest, port, agent)
        await ssh(guest, port, "sudo -n shutdown -h now", check=False)
        with anyio.fail_after(60):
            await vm.wait()
        log(event="prepared", seconds=round(time.monotonic() - start, 1))
    finally:
        await stop(vm, qmp)


@dataclass(frozen=True)
class Started:
    socket: Path
    #: QEMU takes one QMP client at a time, and this is it.
    qmp: Qmp


@contextlib.asynccontextmanager
async def started(snap: Path, run: Path) -> AsyncIterator[Started]:
    """A guest from *snap*, with its daemon served on a Unix socket."""
    if await anyio.Path(run).exists():
        await anyio.to_thread.run_sync(shutil.rmtree, run)
    await anyio.Path(run).mkdir(parents=True)
    t0 = time.monotonic()
    manifest = json.loads(await anyio.Path(snap / "snapshot.json").read_text())
    await anyio.run_process(["cp", "-c", manifest["aux"], str(run / "aux.img")])
    await anyio.run_process(
        [manifest["qemuImg"], "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", manifest["overlay"],
         str(run / "overlay.qcow2")],
    )  # fmt: skip
    args = [arg.replace("{run}", str(run)) for arg in manifest["args"]]
    vm = await launch(run, args, Path(os.environ.get("QEMU", manifest["qemu"])))
    qmp = None
    try:
        qmp = await Qmp.connect(run / "qmp.sock")
        await qmp.call("migrate-set-capabilities", capabilities=X_IGNORE_SHARED)
        await qmp.call("migrate-incoming", uri=f"file:{manifest['state']}")
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
        log(event="ready", loaded_s=round(loaded, 3), daemon_s=round(ready, 3),
            clock_s=round(time.monotonic() - t0, 3))  # fmt: skip
        async with anyio.create_task_group() as tg:
            await tg.start(forward, vsock, run / "daemon.sock")
            yield Started(run / "daemon.sock", qmp)
            tg.cancel_scope.cancel()
    finally:
        await stop(vm, qmp)


async def serve(snap: Path, run: Path) -> None:
    async with started(snap, run) as guest:
        print(f"unix://{guest.socket}?local-fs=false", flush=True)
        await anyio.sleep_forever()


async def build(snap: Path, run: Path, args: list[str]) -> int:
    t0 = time.monotonic()
    async with started(snap, run) as guest:
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
    elif mode == "serve":
        anyio.run(serve, a, b)
    elif mode == "build" and sys.argv[4:5] == ["--"]:
        sys.exit(anyio.run(build, a, b, sys.argv[5:]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
