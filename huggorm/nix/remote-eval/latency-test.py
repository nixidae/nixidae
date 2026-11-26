"""What each step of a warm-server session costs as the link slows
(nixidae/nixidae#738).

The same session runs at each round-trip time: connect, acquire, mount,
a cold evaluation, the same evaluation warm, and two passes that run
their reads again. Each step reports its seconds and its callbacks.
"""

from __future__ import annotations

import json
from typing import Any

from vivarium_runner import Machines

ROOT = "/root/warm"
KEY = "/root/.ssh/id_ed25519"
# One-way delay on each guest's link: a round trip is twice this.
DELAYS_MS = (0, 25, 75, 150)


async def test(vms: Machines) -> None:
    server, client = vms.server, vms.client
    settings: dict[str, Any] = vms.settings  # Any: vivarium's settings, read by key.
    await client.succeed(
        f"test -d {ROOT} || (cp -r --no-preserve=mode {settings['project']} {ROOT} && echo warm > {ROOT}/live.txt)"
    )

    rows = []
    for delay in DELAYS_MS:
        for vm in (server, client):
            await vm.succeed(f"tc qdisc replace dev vec1 root netem delay {delay}ms")
        result = await client.succeed(
            f"{settings['python']} {settings['warm']} sweep --server {settings['server']}"
            f" --socket {settings['socket']} --key {KEY} --root {ROOT}",
            timeout=600,
        )
        steps: dict[str, Any] = json.loads(result.stdout)["steps"]  # Any: Timed as JSON.
        cells = "  ".join(f"{name} {s['seconds']:6.3f}s/{s['callbacks']:>2}" for name, s in steps.items())
        rows.append(f"{2 * delay:>4} ms  {cells}")
    for vm in (server, client):
        await vm.succeed("tc qdisc del dev vec1 root")
    print("[test] rtt  step seconds/callbacks\n" + "\n".join(rows))
