"""A warm server that outlives its clients (nixidae/nixidae#738).

The server guest runs `huggorm.server SOCKET` as a service. Each client
step is a new process that reaches it through OpenSSH Unix socket
forwarding. A client detaches and leaves; the next one claims the state
and mounts the tree again. Then the host breaks the link while the
server waits on a callback: the client must fail within its keepalive
bound, and the server must go on serving.
"""

from __future__ import annotations

import json
from typing import Any

from vivarium_runner import Machine, Machines

ROOT = "/root/warm"
KEY = "/root/.ssh/id_ed25519"
MARKER = "/root/reading"
DROP = "/root/drop.json"
# The client's keepalive gives up after 6 s; the rest is slack for load.
DROP_BOUND_SECONDS = 30


def command(settings: dict[str, Any], step: str, *extra: str) -> str:
    # Any: vivarium's settings, read by key.
    return " ".join(
        [
            settings["python"],
            settings["warm"],
            step,
            f"--server {settings['server']} --socket {settings['socket']} --key {KEY} --root {ROOT}",
            *extra,
        ]
    )


async def step(client: Machine, settings: dict[str, Any], name: str, *extra: str) -> dict[str, Any]:
    # Any: the step's JSON report, read field by field below.
    result = await client.succeed(command(settings, name, *extra), timeout=300)
    report: dict[str, Any] = json.loads(result.stdout)
    print(f"[test] {name}: {json.dumps(report)}")
    return report


async def test(vms: Machines) -> None:
    server, client = vms.server, vms.client
    settings = vms.settings

    for vm in (server, client):
        await vm.succeed("tc qdisc del dev vec1 root || true")
    await server.wait_for_unit("huggorm-server.service")
    await client.succeed(
        f"cp -r --no-preserve=mode {settings['project']} {ROOT}"
        f" && echo warm > {ROOT}/live.txt && echo unread > {ROOT}/unread.txt"
    )

    first = await step(client, settings, "first")
    if first["value"] != 42 or first["files"] != ["/sub/x.nix"]:
        raise AssertionError(f"first: {first}")
    claim = [f"--token {first['token']}", f"--state {first['state']}"]

    again = await step(client, settings, "again", *claim)
    if again["value"]["imported"] != 42 or again["value"]["live"] != "warm\n":
        raise AssertionError(f"again: {again}")
    if "/default.nix" not in again["files"] or "/sub/x.nix" in again["files"]:
        raise AssertionError(f"the claimed state read again what it had: {again['files']}")
    print("[test] a new client process claims the warm state, and reads only what it lacks")

    await client.succeed(
        f"rm -f {MARKER} {DROP}; ({command(settings, 'drop', *claim, f'--marker {MARKER}')} > {DROP} 2> {DROP}.err &)"
    )
    await client.wait_until_succeeds(f"test -e {MARKER}", timeout=60)
    await client.succeed("tc qdisc replace dev vec1 root netem loss 100%")
    print("[test] the link drops while the server waits on the client's read")
    await client.wait_until_succeeds(f"test -s {DROP}", timeout=DROP_BOUND_SECONDS + 30)
    await client.succeed("tc qdisc del dev vec1 root")
    dropped: dict[str, Any] = json.loads((await client.succeed(f"cat {DROP}")).stdout)
    print(f"[test] drop: {json.dumps(dropped)}")
    if not dropped["error_type"] or dropped["seconds"] > DROP_BOUND_SECONDS:
        raise AssertionError(f"the client did not fail within {DROP_BOUND_SECONDS} s: {dropped}")

    fresh = await step(client, settings, "first")
    if fresh["value"] != 42:
        raise AssertionError(f"the server stopped serving after the drop: {fresh}")
    print(f"[test] the server serves a new client after the drop; its log:\n{await server.journal('huggorm-server')}")
