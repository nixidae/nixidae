#!/usr/bin/env python3
"""Throughput through each guest's uplink, from the host.

Each guest runs an iperf3 server behind a forward. The host's iperf3
client reaches it there each way (`--reverse` is guest to host), in
`ROUNDS` rounds that take the guests in turn, and the median counts.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from typing import TypedDict

import anyio
from vivarium_runner import Machine, Machines

DURATION = 5
PORT = 5201
# One sample on a shared CI runner swung an uplink fivefold between two runs.
ROUNDS = 3
READY_WAIT = 30


class _Sum(TypedDict):
    bits_per_second: float


class _End(TypedDict):
    sum_received: _Sum


class IperfReport(TypedDict):
    """The part of `iperf3 --json` that the measurement reads."""

    end: _End


async def measure(iperf3: str, vm: Machine, reverse: bool) -> float:
    """One iperf3 run through *vm*'s forward; bits per second received.

    A run that fails before it moves data is tried again until
    `READY_WAIT`: a forward behind the router can refuse the first
    connections after boot, as `forward.py` allows for."""
    host, port = vm.reachable(PORT)[0].rsplit(":", 1)
    argv = [iperf3, "--client", host, "--port", port, "--time", str(DURATION), "--json"]
    if reverse:
        argv.append("--reverse")
    deadline = anyio.current_time() + READY_WAIT
    while True:
        # A stalled uplink keeps iperf3 waiting forever.
        with anyio.fail_after(DURATION + 30):
            result = await anyio.run_process(argv, check=False)
        report: IperfReport = json.loads(result.stdout)
        if result.returncode == 0:
            return report["end"]["sum_received"]["bits_per_second"]
        if report.get("end") or anyio.current_time() > deadline:
            raise AssertionError(f"{vm.name}: iperf3 failed:\n{result.stdout.decode()}{result.stderr.decode()}")
        await anyio.sleep(0.5)


async def test(vms: Machines) -> None:
    iperf3 = vms.settings["iperf3"]
    # A run's router forwards nothing and runs no server.
    servers = [vm for vm in vms.values() if vm.reachable(PORT)]
    for vm in servers:
        await vm.wait_for_unit("iperf3-server.service")
    mtus = {vm.name: (await vm.succeed("cat /sys/class/net/vec0/mtu")).stdout.strip() for vm in servers}
    rates: defaultdict[tuple[str, str], list[float]] = defaultdict(list)
    for round_ in range(1, ROUNDS + 1):
        for vm in servers:
            for reverse in (False, True):
                rate = await measure(iperf3, vm, reverse)
                direction = "guest -> host" if reverse else "host -> guest"
                rates[vm.name, direction].append(rate)
                print(f"[test] round {round_}: {vm.name}: {direction}: {rate / 1e9:.2f} Gbit/s")
    for (name, direction), samples in rates.items():
        median = statistics.median(samples)
        print(f"[test] {name}: {direction}: {median / 1e9:.2f} Gbit/s median of {ROUNDS} at mtu {mtus[name]}")
