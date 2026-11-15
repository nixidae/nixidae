#!/usr/bin/env agent-python
"""Probe: a writable virtiofs share union-mounted over a read-only, allowlisted one.

The darwin build VM's store: the host store, filtered to a build's inputs, at the store
directory, with a writable host directory over it where the outputs land.

    VFSD=.../darwin-virtiofsd host/probe_union.py GUEST_DIR RUN_DIR SHOWN_PATH HIDDEN_PATH

Boots a throwaway overlay of GUEST_DIR (a prepared guest) with SSH, and prints what each
step in the guest answered.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
from pathlib import Path

import anyio

sys.path.insert(0, str(Path(__file__).resolve().parent))
import darwin_builder as db  # noqa: E402

QEMU = Path(os.environ["QEMU"])
VFSD = os.environ["VFSD"]
TAGS = ("store", "out")
MOUNT = "/private/var/probe/store"


async def server(run: Path, tag: str, shared: Path, *extra: str) -> anyio.abc.Process:
    sock = run / f"{tag}.sock"
    out = await anyio.open_file(run / f"vfsd-{tag}.log", "ab")
    proc = await anyio.open_process(
        [VFSD, "--socket-path", str(sock), "--shared-dir", str(shared), *extra],
        stdout=out.wrapped,
        stderr=out.wrapped,
    )
    with anyio.fail_after(5):
        while not await anyio.Path(sock).exists():
            await anyio.sleep(0.005)
    return proc


async def main(guest: Path, run: Path, shown: str, hidden: str) -> None:
    if run.exists():
        shutil.rmtree(run)
    (run / "out").mkdir(parents=True)
    image = Path((guest / "image").read_text().strip())
    await anyio.run_process(["cp", "-c", str(guest / "aux.img"), str(run / "aux.img")])
    await anyio.run_process(
        [str(QEMU.parent / "qemu-img"), "create", "-q", "-f", "qcow2", "-F", "qcow2",
         "-b", str(guest / "overlay.qcow2"), str(run / "overlay.qcow2")]
    )  # fmt: skip
    (run / "allow").write_text(f"{shown}\n")
    port = db.free_port()
    args = db.qemu_args(image, str(run), "memory-backend-shm", ssh_port=port)
    for tag in TAGS:
        args += ["-chardev", f"socket,id={tag},path={run}/{tag}.sock",
                 "-device", f"vhost-user-fs-pci,chardev={tag},tag={tag}"]  # fmt: skip
    procs = [
        await server(run, "store", Path("/nix/store"), "--allowlist", str(run / "allow")),
        await server(run, "out", run / "out", "--writable"),
    ]
    vm = await db.launch(run, args, QEMU)
    try:
        with anyio.fail_after(120):
            while not await db.ssh(run, port, "true", check=False, connect_timeout=2):
                await anyio.sleep(1)
        name = Path(shown).name
        out = "/private/var/probe/out"
        steps = {
            "mount store": f"mkdir -p {MOUNT} && mount_virtiofs store {MOUNT}",
            "shown": f"ls {MOUNT}/{name} | head -3",
            "hidden": f"ls -d {MOUNT}/{Path(hidden).name}",
            "listing": f"ls {MOUNT}",
            "union tmpfs": f"mount_tmpfs -o union {MOUNT}",
            "mounts": f"mount | grep {MOUNT}",
            "shown after union": f"ls {MOUNT}/{name} | head -3",
            "hidden after union": f"ls -d {MOUNT}/{Path(hidden).name}",
            "run from the store": f"{MOUNT}/{name}/bin/python3 -c \"print(1)\"",
            "create output": f"mkdir {MOUNT}/zzz-out && echo built > {MOUNT}/zzz-out/file"
            f" && printf \"#!/bin/sh\\n\" > {MOUNT}/zzz-out/exe && chmod 755 {MOUNT}/zzz-out/exe"
            f" && ln -s file {MOUNT}/zzz-out/link && cat {MOUNT}/zzz-out/file",
            "write into an input": f"touch {MOUNT}/{name}/probe",
            "remove an input": f"rm -rf {MOUNT}/{name}",
            "shown at the end": f"ls {MOUNT}/{name} | head -3",
            "mount out": f"mkdir -p {out} && mount_virtiofs out {out}",
            "source modes": f"ls -l {MOUNT}/zzz-out",
            "create on out": f"cd {out} && umask 022 && echo x > plain && stat -f %Lp plain"
            " && chmod 600 plain && stat -f %Lp plain && mkdir d && stat -f %Lp d",
            "copy out": f"cp -Rp {MOUNT}/zzz-out {out}/ && ls -lR {out}",
        }
        results = {}
        for label, command in steps.items():
            proc = await anyio.run_process(
                ["ssh", "-p", str(port), "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                 "-o", "LogLevel=ERROR", "admin@127.0.0.1", f"sudo -n sh -c '{command}'"],
                env=os.environ | {"SSH_ASKPASS": str(run / "askpass"), "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": ":0"},
                check=False,
            )  # fmt: skip
            results[label] = {
                "rc": proc.returncode,
                "out": proc.stdout.decode(errors="replace").strip()[-400:],
                "err": proc.stderr.decode(errors="replace").strip()[-400:],
            }
        host = {str(p.relative_to(run / "out")): oct(p.lstat().st_mode) for p in (run / "out").rglob("*")}
        print(json.dumps({"guest": results, "host out": host}, indent=1))
    finally:
        with anyio.CancelScope(shield=True):
            vm.kill()
            await vm.wait()
            for proc in procs:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                await proc.wait()


if __name__ == "__main__":
    anyio.run(main, Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4])
