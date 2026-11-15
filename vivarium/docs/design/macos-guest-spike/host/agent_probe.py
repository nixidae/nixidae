#!/usr/bin/env agent-python
"""Run vivarium's own guest agent in a macOS guest, and drive it from the host.

    agent_probe.py GUEST_DIR RUN_DIR AGENT_STORE_PATH
    agent_probe.py --snapshot SNAP_DIR RUN_DIR AGENT_STORE_PATH

AGENT_STORE_PATH is vivarium-runner built for aarch64-darwin. The probe boots
a clone of GUEST_DIR, copies the agent's closure in over the vsock daemon,
starts `vivarium-agent` on /dev/cu.vivarium-agent (the virtio-serial port),
then connects arpyc to RUN_DIR/agent.sock and calls `run`.

With --snapshot, SNAP_DIR's guest already runs the agent (`prepare` with
AGENT): the probe times a start until the agent's first answer.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import anyio

sys.path.insert(0, str(Path(__file__).resolve().parent))
import darwin_builder as db


async def import_path(agent: str) -> None:
    closure = await anyio.run_process(["nix", "path-info", "--recursive", agent])
    for path in closure.stdout.decode().split():
        sys.path.insert(0, f"{path}/lib/python3.14/site-packages")


async def from_snapshot(snap: Path, run: Path, agent: str) -> int:
    await import_path(agent)
    from vivarium_runner import arpyc

    t0 = time.monotonic()
    async with db.started(snap, run):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(run / "agent.sock"))
        conn = arpyc.connect(sock.detach())
        with anyio.fail_after(30):
            code, out = await conn.call(
                "run", ("sw_vers -productVersion; date -u",), {}
            )
        first = time.monotonic() - t0
        conn.close()
    print(json.dumps({"code": code, "out": out, "first_answer_s": round(first, 3)}))
    return 0 if code == 0 else 1


async def main() -> int:
    if sys.argv[1] == "--snapshot":
        return await from_snapshot(
            Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve(), sys.argv[4]
        )
    guest, run, agent = (
        Path(sys.argv[1]).resolve(),
        Path(sys.argv[2]).resolve(),
        sys.argv[3],
    )
    await import_path(agent)
    from vivarium_runner import arpyc

    await anyio.Path(run).mkdir(parents=True)
    for name in ("overlay.qcow2", "aux.img"):
        await anyio.run_process(["cp", "-c", str(guest / name), str(run / name)])
    image = (await anyio.Path(guest / "image").read_text()).strip()
    port = db.free_port()
    vm = await db.launch(
        run, db.qemu_args(Path(image), str(run), "memory-backend-ram", ssh_port=port)
    )
    qmp = None
    results: dict[str, object] = {}
    try:
        qmp = await db.Qmp.connect(run / "qmp.sock")
        t0 = time.monotonic()
        while not await db.daemon_answers(run / "vsock.sock"):
            await anyio.sleep(0.5)
        results["daemon_s"] = round(time.monotonic() - t0, 1)
        async with anyio.create_task_group() as tg:
            await tg.start(db.forward, run / "vsock.sock", run / "daemon.sock")
            await anyio.run_process(
                ["pynix", "copy", "--no-check-sigs", "--to", f"unix://{run / 'daemon.sock'}?local-fs=false", agent],
                stdout=subprocess.DEVNULL, stderr=None,
            )  # fmt: skip
            tg.cancel_scope.cancel()
        guest_path = "/usr/bin:/bin:/usr/sbin:/sbin:/nix/var/nix/profiles/default/bin"
        await db.ssh(run, port, f"sudo -n sh -c 'VIVARIUM_AGENT_DEVICE=/dev/cu.vivarium-agent "
                     f"VIVARIUM_GUEST_PATH={guest_path} nohup {agent}/bin/vivarium-agent "
                     f"> /tmp/vivarium-agent.log 2>&1 &'")  # fmt: skip
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(run / "agent.sock"))
        conn = arpyc.connect(sock.detach())
        with anyio.fail_after(30):
            t1 = time.monotonic()
            code, out = await conn.call(
                "run", ("sw_vers -productVersion; id -un; nix --version",), {}
            )
            results["run"] = {
                "code": code,
                "out": out,
                "seconds": round(time.monotonic() - t1, 2),
            }
            code, out = await conn.call("run", ("exit 3",), {})
            results["exit_code"] = code
            t2 = time.monotonic()
            for _ in range(20):
                await conn.call("run", ("true",), {})
            results["round_trip_ms"] = round((time.monotonic() - t2) / 20 * 1000, 1)
        conn.close()
    finally:
        await db.stop(vm, qmp)
    print(json.dumps(results))
    ok = results.get("run", {}).get("code") == 0 and results.get("exit_code") == 3  # type: ignore[union-attr]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(anyio.run(main))
