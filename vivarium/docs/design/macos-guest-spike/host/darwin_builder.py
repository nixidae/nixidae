#!/usr/bin/env agent-python
"""A macOS builder VM per build, started from a snapshot.

    darwin_builder.py save GUEST_DIR SNAP_DIR     boot GUEST_DIR, wait for its daemon, save
    darwin_builder.py serve SNAP_DIR RUN_DIR      start from SNAP_DIR; RUN_DIR/daemon.sock until ^C
    darwin_builder.py build SNAP_DIR RUN_DIR -- PYNIX_BUILD_ARGS...

GUEST_DIR is a configured guest (overlay.qcow2, aux.img) with the builder
LaunchDaemons: nix-daemon --stdio on vsock port 3030, the clock on 3031.

SNAP_DIR holds the disk overlay as it was at the save (read only from then on),
aux.img, ram.bin (guest RAM, a shared memory-backend-file) and state.bin (device
state; x-ignore-shared leaves RAM out of it). A start maps ram.bin private and
copy-on-write, so pages fault in on first touch and the file never changes, and
puts a fresh qcow2 overlay over the saved one. Every start is the saved guest.

`build` runs `pynix build` against the guest's daemon and copies the outputs
into the host store, then stops the guest. Timings go to stderr as JSON.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import struct
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from anyio.abc import ByteStream, Process, SocketStream

HERE = Path(__file__).resolve().parent
QEMU_IMG = Path(os.environ.get("QEMU_IMG", Path.home() / "Dynamist/qemu/build-hvf/qemu-img"))
RAM = os.environ.get("RAM", "8G")
IMG = Path(os.environ.get("IMG", Path.home() / "Dynamist/orchard-images/sonoma"))
DAEMON_PORT = 3030
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
    await stream.send(f"CONNECT {port}\n".encode())
    line = b""
    while not line.endswith(b"\n"):
        line += await stream.receive(1)
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
    except (OSError, anyio.EndOfStream, anyio.BrokenResourceError):
        return False
    return len(reply) == WORD.size and WORD.unpack(reply)[0] == WORKER_MAGIC_2


async def set_clock(vsock: Path) -> None:
    async with await connect_guest(vsock, CLOCK_PORT) as stream:
        await stream.send(f"{int(time.time())}\n".encode())
        await stream.send_eof()
        reply = b""
        with contextlib.suppress(anyio.EndOfStream):
            while True:
                reply += await stream.receive()
    if reply.strip() != b"ok":
        msg = f"the guest did not set its clock: {reply!r}"
        raise RuntimeError(msg)


async def pump(source: ByteStream, sink: ByteStream) -> None:
    with contextlib.suppress(anyio.BrokenResourceError, anyio.ClosedResourceError, anyio.EndOfStream):
        async for chunk in source:
            await sink.send(chunk)
    # Half-close, so the far side sees EOF and still answers.
    with contextlib.suppress(anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
        await sink.send_eof()


async def forward(vsock: Path, listen: Path, *, task_status: anyio.abc.TaskStatus[None]) -> None:
    """Serve the guest daemon on *listen*, a plain Unix socket Nix can use."""
    await anyio.Path(listen).unlink(missing_ok=True)
    listener = await anyio.create_unix_listener(listen)

    async def handle(client: SocketStream) -> None:
        async with client, await connect_guest(vsock, DAEMON_PORT) as guest, anyio.create_task_group() as tg:
            tg.start_soon(pump, client, guest)
            tg.start_soon(pump, guest, client)

    task_status.started()
    await listener.serve(handle)


async def launch(run: Path, ram: str, *extra: str) -> Process:
    env = os.environ | {"IMG": str(IMG), "RUN": str(run), "RAM": RAM, "SSH_PORT": os.environ.get("SSH_PORT", "")}
    command = [
        str(HERE / "run-hvf.sh"),
        "-object", f"memory-backend-file,id=ram0,size={RAM},{ram}",
        "-machine", "memory-backend=ram0",
        *extra,
    ]  # fmt: skip
    out = await anyio.open_file(run / "stdout.log", "ab")
    return await anyio.open_process(command, env=env, stdout=out.wrapped, stderr=out.wrapped)


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


async def save(guest: Path, snap: Path) -> None:
    await anyio.Path(snap).mkdir(parents=True)
    for name in ("overlay.qcow2", "aux.img"):
        await anyio.run_process(["cp", "-c", str(guest / name), str(snap / name)])
    vm = await launch(snap, f"mem-path={snap / 'ram.bin'},share=on")
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
        # Let launchd settle, so the snapshot is of an idle guest.
        await anyio.sleep(float(os.environ.get("SETTLE", "30")))
        await qmp.call("stop")
        await qmp.call("migrate-set-capabilities", capabilities=X_IGNORE_SHARED)
        await qmp.call("migrate", uri=f"file:{snap / 'state.bin'}")
        while True:
            status = await qmp.call("query-migrate")
            if isinstance(status, dict) and status.get("status") in ("completed", "failed", "cancelled"):
                break
            await anyio.sleep(0.05)
        log(event="saved", status=status.get("status"))
        if status.get("status") != "completed":
            sys.exit(f"migration failed: {status}")
    finally:
        await stop(vm, qmp)
    await anyio.Path(snap / "overlay.qcow2").chmod(0o444)
    print(json.dumps({"snapshot": str(snap), "state_bytes": (await anyio.Path(snap / "state.bin").stat()).st_size}))


@contextlib.asynccontextmanager
async def started(snap: Path, run: Path) -> AsyncIterator[Path]:
    """A guest from *snap*; yields the Unix socket of its daemon."""
    if await anyio.Path(run).exists():
        await anyio.to_thread.run_sync(shutil.rmtree, run)
    await anyio.Path(run).mkdir(parents=True)
    t0 = time.monotonic()
    await anyio.run_process(["cp", "-c", str(snap / "aux.img"), str(run / "aux.img")])
    await anyio.run_process(
        [str(QEMU_IMG), "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", str(snap / "overlay.qcow2"),
         str(run / "overlay.qcow2")],
    )  # fmt: skip
    # Private: with a MAP_SHARED mapping the host pager writes dirty guest pages
    # back to the file, and the guest stalled for 5-25 s at a time. The stream
    # carries no RAM, so the destination keeps the file's pages.
    vm = await launch(run, f"mem-path={snap / 'ram.bin'},share=off,readonly=on,rom=off", "-incoming", "defer")
    qmp = None
    try:
        qmp = await Qmp.connect(run / "qmp.sock")
        await qmp.call("migrate-set-capabilities", capabilities=X_IGNORE_SHARED)
        await qmp.call("migrate-incoming", uri=f"file:{snap / 'state.bin'}")
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
            yield run / "daemon.sock"
            tg.cancel_scope.cancel()
    finally:
        await stop(vm, qmp)


async def serve(snap: Path, run: Path) -> None:
    async with started(snap, run) as socket:
        print(f"unix://{socket}?local-fs=false", flush=True)
        await anyio.sleep_forever()


async def build(snap: Path, run: Path, args: list[str]) -> int:
    t0 = time.monotonic()
    async with started(snap, run) as socket:
        command = ["pynix", "build", "--store", f"unix://{socket}?local-fs=false", "--eval-store", "auto",
                   "--copy", "auto", *args]  # fmt: skip
        result = await anyio.run_process(command, stdout=None, stderr=None, check=False)
        built = time.monotonic() - t0
    log(event="done", build_s=round(built, 3), total_s=round(time.monotonic() - t0, 3), status=result.returncode)
    return result.returncode


def main() -> None:
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    mode, a, b = sys.argv[1], Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve()
    if mode == "save":
        anyio.run(save, a, b)
    elif mode == "serve":
        anyio.run(serve, a, b)
    elif mode == "build" and sys.argv[4:5] == ["--"]:
        sys.exit(anyio.run(build, a, b, sys.argv[5:]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
