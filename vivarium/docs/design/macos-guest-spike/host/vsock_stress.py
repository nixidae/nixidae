#!/usr/bin/env agent-python
"""Stress virtio-vsock-unix through a builder guest's daemon.

    vsock_stress.py SNAP_DIR RUN_DIR [BIG_STORE_PATH]

1. 64 connections at once, each a full `nix path-info --all` over the daemon:
   every answer must be the same.
2. BIG_STORE_PATH's closure into the guest and its NAR hashes back.
3. A guest reset with 16 connections open: each must end, and QEMU must live.

Prints one JSON line per check; exits 1 when one fails.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import anyio

sys.path.insert(0, str(Path(__file__).resolve().parent))
import darwin_builder


async def path_info_all(store: str) -> bytes:
    result = await anyio.run_process(["nix", "path-info", "--all", "--store", store], check=False)
    if result.returncode != 0:
        msg = f"nix path-info failed: {result.stderr.decode(errors='replace')[-500:]}"
        raise RuntimeError(msg)
    return result.stdout


async def concurrent(store: str, count: int) -> dict[str, object]:
    answers: list[bytes] = []
    errors: list[str] = []
    t0 = time.monotonic()

    async def one() -> None:
        try:
            answers.append(await path_info_all(store))
        except RuntimeError as exc:
            errors.append(str(exc))

    async with anyio.create_task_group() as tg:
        for _ in range(count):
            tg.start_soon(one)
    distinct = len(set(answers))
    return {"check": "concurrent", "connections": count, "ok": not errors and distinct == 1,
            "distinct_answers": distinct, "errors": errors[:3], "paths": answers[0].count(b"\n") if answers else 0,
            "seconds": round(time.monotonic() - t0, 2)}  # fmt: skip


async def big_copy(store: str, path: str) -> dict[str, object]:
    t0 = time.monotonic()
    copied = await anyio.run_process(
        ["pynix", "copy", "--no-check-sigs", "--to", store, path],
        check=False,
        stderr=None,
    )
    seconds = time.monotonic() - t0
    # Only what crossed: paths the guest already had cost no transfer.
    sent = json.loads(copied.stdout).get("copied", []) if copied.returncode == 0 else []
    sizes = await anyio.run_process(["nix", "path-info", "--json", *sent]) if sent else None
    sent_bytes = sum(info["narSize"] for info in json.loads(sizes.stdout).values()) if sizes else 0
    host = await anyio.run_process(["nix", "path-info", "--json", "--recursive", path])
    guest = await anyio.run_process(
        ["nix", "path-info", "--json", "--recursive", "--store", store, path],
        check=False,
    )

    def hashes(raw: bytes) -> dict[str, str]:
        return {p: info["narHash"] for p, info in json.loads(raw).items() if info}

    same = guest.returncode == 0 and hashes(host.stdout) == hashes(guest.stdout)
    return {"check": "big_copy", "ok": copied.returncode == 0 and same,
            "seconds": round(seconds, 2), "paths_sent": len(sent),
            "bytes_sent": sent_bytes, "mb_per_s": round(sent_bytes / seconds / 1e6, 1)}  # fmt: skip


async def reset_with_open_connections(socket: Path, qmp: darwin_builder.Qmp) -> dict[str, object]:
    """Open daemon connections, reset the guest, and see that each one ends."""
    streams = []
    for _ in range(16):
        stream = await anyio.connect_unix(socket)
        await stream.send(darwin_builder.WORD.pack(darwin_builder.WORKER_MAGIC_1))
        streams.append(stream)
    # Each connection's handshake reply proves it reached the guest first.
    for stream in streams:
        with anyio.fail_after(5):
            await stream.receive(8)
    await qmp.call("system_reset")
    ended = 0
    with anyio.move_on_after(30):
        for stream in streams:
            try:
                while await stream.receive():
                    pass
            except anyio.EndOfStream, anyio.BrokenResourceError, OSError:
                ended += 1
    status = await qmp.status()
    return {"check": "reset", "ok": ended == len(streams) and status == "running", "ended": ended,
            "open": len(streams), "qemu": status}  # fmt: skip


async def main() -> int:
    snap, run = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    big = sys.argv[3] if len(sys.argv) > 3 else None
    results = []
    async with darwin_builder.started(snap, run) as guest:
        store = f"unix://{guest.socket}?local-fs=false"
        results.append(await concurrent(store, 64))
        print(json.dumps(results[-1]), flush=True)
        if big:
            results.append(await big_copy(store, big))
            print(json.dumps(results[-1]), flush=True)
        results.append(await reset_with_open_connections(guest.socket, guest.qmp))
        print(json.dumps(results[-1]), flush=True)
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(anyio.run(main))
