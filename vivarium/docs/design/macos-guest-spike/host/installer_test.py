#!/usr/bin/env agent-python
"""pynixd's macOS installer on a fresh macOS guest, as a GitHub macOS runner runs it.

    installer_test.py TARBALL_DIR RUN_DIR [--volume case-insensitive|case-sensitive] [--keep]

TARBALL_DIR holds `pynixd-aarch64-darwin.tar.xz` (`internal.installer` of
pynixd). The guest is a new qcow2 overlay over IMG (default: the sequoia tart
image, macOS 15, as `macos-latest`), so every run starts with no /nix. The
step it runs is the one the workflow renders: the `run` and `env` of
"Install pynixd as the Nix daemon" in `.github/workflows/pynixd-installer.yml`,
with the `${{ }}` expressions GitHub would fill in. Then the runner job's
checks of the daemon and the volume. `--keep` leaves the guest up, with
`RUN_DIR/ssh` to reach it; `--shell` boots it and installs nothing.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
import time
from pathlib import Path

import anyio
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import darwin_builder as db

WORKFLOW = (
    Path(__file__).resolve().parents[5] / ".github/workflows/pynixd-installer.yml"
)
TARBALL = "pynixd-aarch64-darwin.tar.xz"
GUEST_TARBALL_DIR = "/Users/admin/pynixd-tarball"


def install_step(volume: str) -> tuple[str, dict[str, str]]:
    """The runner job's install step: its script and its environment, expressions filled in."""
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["runner"]
    step = next(
        s for s in job["steps"] if s.get("name") == "Install pynixd as the Nix daemon"
    )
    values = {
        "matrix.volume": volume,
        "github.token": "local-test",
        "runner.temp": "/Users/admin/runner-temp",
    }

    def fill(text: str) -> str:
        def one(match: re.Match[str]) -> str:
            name = match.group(1).strip()
            if name not in values:
                raise SystemExit(
                    f"installer_test: the step reads ${{{{ {name} }}}}, which this test does not fill"
                )
            return values[name]

        return re.sub(r"\$\{\{([^}]*)\}\}", one, text)

    env = {name: fill(str(value)) for name, value in step.get("env", {}).items()}
    # The tarball job's artifact, downloaded where the runner job puts it.
    env["PYNIXD_TARBALL"] = f"file://{GUEST_TARBALL_DIR}"
    return fill(step["run"]), env


CHECKS = """
set -eo pipefail
export PATH=/nix/var/nix/profiles/default/bin:$PATH
sudo launchctl print system/org.nixidae.pynixd > /dev/null
! pgrep -x nix-daemon
nix store info
if [ "$VOLUME" = case-sensitive ]; then fs="Case-sensitive APFS" hack=false; else fs=APFS hack=true; fi
test "$(diskutil info -plist /nix | plutil -extract FilesystemName raw -)" = "$fs"
test "$(nix config show use-case-hack)" = "$hack"
# A build with no substitute: pynixd builds it, as a _nixbld user.
out=$(nix build --no-link --print-out-paths --expr '(derivation { name = "installer-test"; system = "aarch64-darwin"; builder = "/bin/sh"; args = [ "-c" "/usr/bin/id -un > $out; echo $RANDOM >> $out" ]; })' --impure)
head -1 "$out" | grep -q '^_nixbld'
echo "built $out as $(head -1 "$out")"
"""


async def guest_run(run: Path, port: int, script: str, env: dict[str, str]) -> None:
    exports = "".join(
        f"export {name}={shlex.quote(value)}\n" for name, value in env.items()
    )
    body = run / "step.sh"
    await anyio.Path(body).write_text("set -eo pipefail\n" + exports + script)
    await db.ssh(run, port, "--", str(body), "/tmp/step.sh")
    await db.ssh(run, port, "bash /tmp/step.sh")


async def main(tarballs: Path, run: Path, volume: str, keep: bool, shell: bool) -> None:
    script, env = install_step(volume)
    # QEMU's sockets live in RUN_DIR, and darwin's `sun_path` holds 103 bytes.
    if len(str(await anyio.Path(run).resolve() / "vsock.sock")) > 103:
        raise SystemExit(
            f"installer_test: {run} is too long a path for QEMU's sockets; take a shorter one"
        )
    img = db.IMG
    await anyio.Path(run).mkdir(parents=True, exist_ok=True)
    disk, disk_format = db.base_disk(img)
    overlay = run / "overlay.qcow2"
    await anyio.Path(overlay).unlink(missing_ok=True)
    await anyio.run_process([str(db.QEMU_IMG), "create", "-q", "-f", "qcow2", "-F", disk_format, "-b",
                             str(disk), str(overlay)])  # fmt: skip
    await anyio.run_process(["cp", "-c", str(img / "aux.img"), str(run / "aux.img")])
    port = db.free_port()
    # tart's admin/admin, through the askpass `db.ssh` writes.
    await anyio.Path(run / "ssh").write_text(
        f"#!/bin/sh\nSSH_ASKPASS={run}/askpass SSH_ASKPASS_REQUIRE=force DISPLAY=:0 exec ssh -p {port} "
        '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR admin@127.0.0.1 "$@"\n'
    )
    await anyio.Path(run / "ssh").chmod(0o755)
    # A register HVF hands to QEMU and QEMU does not know becomes an UNDEF in
    # the guest; these name it.
    trace = ["-trace", "hvf_unhandled_sysreg_*", "-D", str(run / "qemu-trace.log")]
    if os.environ.get("TRACE_SYSREGS") == "1":
        trace += ["-trace", "hvf_sysreg_*"]
    vm = await db.launch(
        run,
        db.qemu_args(img, str(run), "memory-backend-ram", ssh_port=port) + trace,
    )
    qmp = None
    start = time.monotonic()
    try:
        qmp = await db.Qmp.connect(run / "qmp.sock")
        with anyio.fail_after(300):
            while not await db.ssh(run, port, "true", check=False, connect_timeout=5):
                if vm.returncode is not None:
                    raise SystemExit(
                        f"QEMU exited with {vm.returncode}; see {run}/stdout.log"
                    )
                await anyio.sleep(1)
        db.log(event="ssh", seconds=round(time.monotonic() - start, 1))
        # tart's daemon reboots the guest when tart's host side is absent.
        await db.ssh(run, port, "sudo -n launchctl disable system/org.cirruslabs.tart-guest-daemon; "
                     "sudo -n launchctl bootout system/org.cirruslabs.tart-guest-daemon || true")  # fmt: skip
        if shell:
            print(
                f"the guest is up, nothing installed: {run}/ssh; ^C stops it",
                file=sys.stderr,
            )
            await anyio.sleep_forever()
        await db.ssh(
            run,
            port,
            f"mkdir -p {GUEST_TARBALL_DIR} /Users/admin/runner-temp && test ! -e /nix",
        )
        await db.ssh(run, port, "--", str(tarballs / TARBALL), f"{GUEST_TARBALL_DIR}/")
        guest_env = {
            "RUNNER_OS": "macOS",
            "RUNNER_TEMP": "/Users/admin/runner-temp",
            "GITHUB_PATH": "/Users/admin/runner-temp/github-path",
            **env,
        }
        step = time.monotonic()
        await guest_run(run, port, script, guest_env)
        db.log(
            event="installed", volume=volume, seconds=round(time.monotonic() - step, 1)
        )
        await guest_run(run, port, CHECKS, {"VOLUME": volume})
        db.log(
            event="checked", volume=volume, seconds=round(time.monotonic() - start, 1)
        )
        if keep:
            print(f"the guest is up: {run}/ssh; ^C stops it", file=sys.stderr)
            await anyio.sleep_forever()
    finally:
        if vm.returncode is None:
            with anyio.CancelScope(shield=True), anyio.move_on_after(20):
                log = await anyio.run_process(
                    [str(run / "ssh"), "sudo -n cat /var/log/pynixd.log"], check=False
                )
                await anyio.Path(run / "pynixd.log").write_bytes(log.stdout)
        await db.stop(vm, qmp)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tarballs", type=Path)
    parser.add_argument("run", type=Path)
    parser.add_argument(
        "--volume",
        choices=("case-insensitive", "case-sensitive"),
        default="case-insensitive",
    )
    parser.add_argument("--keep", action="store_true")
    parser.add_argument(
        "--shell", action="store_true", help="boot the guest and install nothing"
    )
    args = parser.parse_args()
    os.environ.setdefault("IMG", str(Path.home() / "Dynamist/orchard-images/sequoia"))
    db.IMG = Path(os.environ["IMG"])
    anyio.run(main, args.tarballs, args.run, args.volume, args.keep, args.shell)
