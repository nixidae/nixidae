#!/usr/bin/env python3
"""Throughput through each guest's uplink, from the host.

Each guest runs an iperf3 server behind a forward. The host's iperf3
client reaches it there, once each way (`--reverse` is guest to host).
"""

import json
from typing import TypedDict

import anyio

from vivarium_runner import Machines

DURATION = 5
PORT = 5201


class _Sum(TypedDict):
    bits_per_second: float


class _End(TypedDict):
    sum_received: _Sum


class IperfReport(TypedDict):
    """The part of `iperf3 --json` that the measurement reads."""

    end: _End


async def test(vms: Machines) -> None:
    iperf3 = vms.settings["iperf3"]
    for vm in vms.values():
        await vm.wait_for_unit("iperf3-server.service")
    for vm in vms.values():
        host, port = vm.reachable(PORT)[0].rsplit(":", 1)
        mtu = (await vm.succeed("cat /sys/class/net/vec0/mtu")).strip()
        for reverse in (False, True):
            argv = [iperf3, "--client", host, "--port", port, "--time", str(DURATION), "--json"]
            if reverse:
                argv.append("--reverse")
            # A stalled uplink keeps iperf3 waiting forever.
            with anyio.fail_after(DURATION + 30):
                result = await anyio.run_process(argv, check=False)
            if result.returncode != 0:
                raise AssertionError(f"{vm.name}: iperf3 failed:\n{result.stdout.decode()}{result.stderr.decode()}")
            report: IperfReport = json.loads(result.stdout)
            rate = report["end"]["sum_received"]["bits_per_second"]
            direction = "guest -> host" if reverse else "host -> guest"
            print(f"[test] {vm.name}: {direction}: {rate / 1e9:.2f} Gbit/s at mtu {mtu}")
