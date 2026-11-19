#!/usr/bin/env python3
"""One guest, reached from the host.

Everything else here drives guests over the serial line, which works
before networking exists and says nothing about whether anyone outside
can reach a service inside.  This is the test that connects the other
way: the host opens a TCP connection to an address passt is listening on
and expects a process in the guest to answer it.

The three things worth guarding, in order of how quietly they break:

  * a listed port bound in the guest after boot is reachable: passt and
    the egress listen on it from the start;
  * a privileged guest port is reachable at a host port anyone may bind,
    because nothing here may bind below `net.ipv4.ip_unprivileged_port_start`;
  * a port nobody listed is *not* reachable, and says so rather than
    appearing to work.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import anyio
from vivarium_runner import Machine, Machines

UNLISTED = 9090
"""Served in the guest, and in no forward."""

PAGE = "hello-from-inside-the-guest"
DOC_ROOT = "/tmp/www"

PLAIN = 8080
"""An ordinary port, bound long after passt stopped taking arguments."""

PRIVILEGED = 80
"""Bound in the guest as root; its forward takes any free host port,
which is one the host lets anyone bind."""


async def serve(vm: Machine, port: int) -> None:
    """Start a web server in the guest on *port* and leave it running."""
    await vm.succeed(f"mkdir -p {DOC_ROOT} && echo {PAGE} > {DOC_ROOT}/probe")
    await vm.succeed(
        f"systemd-run --unit=probe-{port} --collect --working-directory={DOC_ROOT} python3 -m http.server {port}"
    )


def _get(where: str) -> str:
    body: bytes = urllib.request.urlopen(f"http://{where}/probe", timeout=5).read()
    return body.decode().strip()


async def fetch(where: str, timeout: float = 30) -> str:
    """GET /probe from the host side, giving the guest time to bind."""
    deadline = anyio.current_time() + timeout
    while True:
        try:
            return await anyio.to_thread.run_sync(_get, where)
        except (urllib.error.URLError, OSError) as error:
            if anyio.current_time() > deadline:
                raise AssertionError(f"nothing answered on {where}: {error}") from error
            await anyio.sleep(0.5)


async def test(vms: Machines) -> None:
    vm = vms.node

    # `Rule.address` is None until `resolve_forward` fills it in.
    address = vm.forward[0].address
    print(f"[test] guest was given {address}")
    assert address is not None, "the guest booted with an unresolved forward"
    assert address.startswith("127.0.0."), f"expected a loopback address, got {address}"

    await vm.wait_for_unit("sshd.service")
    assert vm.reachable(vm.spec.ssh_port), "sshd's port is not forwarded"

    # Nothing has bound these yet, and passt has been running since
    # before the guest booted.
    for port in (PLAIN, PRIVILEGED, UNLISTED):
        await serve(vm, port)

    # The host port is the runner's to say: a build binds any free one
    # (nixidae/nixidae#1546).
    where = vm.reachable(PLAIN)
    assert len(where) == 1 and where[0].startswith(f"{address}:"), where
    assert await fetch(where[0]) == PAGE
    print(f"[test] port bound after boot answers on {where[0]}")

    where = vm.reachable(PRIVILEGED)
    assert len(where) == 1 and where[0].startswith(f"{address}:"), where
    assert int(where[0].rpartition(":")[2]) >= 1024, where
    assert await fetch(where[0]) == PAGE
    print(f"[test] privileged {PRIVILEGED} answers on {where[0]}")

    # No fetch waited for UNLISTED's server, which has no forward.
    await vm.wait_until_succeeds(f"ss -Htln 'sport = :{UNLISTED}' | grep -q .", timeout=30)
    listening = await vm.listening()
    for port in (PLAIN, PRIVILEGED, UNLISTED, vm.spec.ssh_port):
        assert port in listening, f"{port} missing from {listening}"
    print(f"[test] guest reports listening on {', '.join(map(str, listening))}")

    # A service comes up fine and is simply unreachable; saying so is the point.
    assert vm.reachable(UNLISTED) == [], f"{UNLISTED} should not be forwarded"
    print(f"[test] unlisted {UNLISTED} correctly reports as not forwarded")
