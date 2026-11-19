"""A launch waits on its helpers without holding the event loop, and owns each one from its start.

A sync wait here held the loop: guests launched one after another, and
the runner did nothing else meanwhile (nixidae/nixidae#1495).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import anyio
from vivarium_runner import forward
from vivarium_runner.backends.host import abandon, start_egress
from vivarium_runner.forward import PortMap, Rule

EGRESS = f"""#!{sys.executable}
import socket, sys, time
from pathlib import Path
ready, go = Path(sys.argv[1]), Path(sys.argv[3])
fd = int(next(arg for arg in sys.argv if arg.startswith("--control=")).split("=")[1])
while not go.exists():
    time.sleep(0.01)
ready.touch()
control = socket.socket(fileno=fd)
while True:
    line, fds, _, _ = socket.recv_fds(control, 512, 1)
    if not line:
        break
    listener = socket.socket(fileno=fds[0])
    control.sendall(b"ok\\n")
    client, _ = listener.accept()
    client.sendall(line)
    client.close()
"""
"""An egress that listens once *go* exists: argv is listen, gateway, then
the egress's args, then `--control=FD`. Each socket it takes answers its
first connection with the line it came with."""


def _egress(tmp_path: Path) -> Path:
    egress = tmp_path / "egress"
    egress.write_text(EGRESS)
    egress.chmod(0o755)
    return egress


def test_another_task_runs_while_the_egress_starts(tmp_path: Path) -> None:
    egress, ready, go = _egress(tmp_path), tmp_path / "ready", tmp_path / "go"
    helpers: list[subprocess.Popen[bytes]] = []

    async def main() -> None:
        try:
            with anyio.fail_after(5):
                async with anyio.create_task_group() as tg:
                    tg.start_soon(start_egress, egress, tmp_path, "10.0.2.2", str(ready), ready, [str(go)], [], helpers)
                    # The start runs first, up to its wait. The egress
                    # listens only after the touch: a wait that held the
                    # loop would wait out its own deadline.
                    await anyio.lowlevel.checkpoint()
                    await anyio.Path(go).touch()
        finally:
            abandon(helpers, ())

    anyio.run(main)
    assert ready.exists()


def test_a_cancelled_start_leaves_its_helper_to_the_launch(tmp_path: Path) -> None:
    egress, ready = _egress(tmp_path), tmp_path / "ready"
    helpers: list[subprocess.Popen[bytes]] = []

    async def main() -> None:
        with anyio.move_on_after(0.2):
            await start_egress(egress, tmp_path, "10.0.2.2", str(ready), ready, [str(tmp_path / "never")], [], helpers)

    anyio.run(main)
    [helper] = helpers
    assert helper.poll() is None
    abandon(helpers, ())
    assert helper.poll() is not None


def test_the_egress_serves_the_socket_the_runner_claimed(tmp_path: Path) -> None:
    """The runner binds, the egress serves: a connection to the claimed port
    reaches the egress after the runner let go of its own copy."""
    egress, ready, go = _egress(tmp_path), tmp_path / "ready", tmp_path / "go"
    go.touch()
    [rule], claims = forward.claim([Rule(address="127.0.0.1", ports=(PortMap(4325, 22),))], random=True)
    port = rule.ports[0].host
    if port is None:
        raise AssertionError("claim() gives every port a number")
    helpers: list[subprocess.Popen[bytes]] = []

    async def main() -> bytes:
        try:
            adds = forward.egress_adds(claims, "10.0.2.16", "/r")
            with await start_egress(egress, tmp_path, "10.0.2.2", str(ready), ready, [str(go)], adds, helpers):
                for held in claims:
                    held.sock.close()
                with anyio.fail_after(5):
                    async with await anyio.connect_tcp("127.0.0.1", port) as stream:
                        return await stream.receive()
        finally:
            abandon(helpers, ())

    assert anyio.run(main) == b"add tcp 10.0.2.16:22 /r\n"
