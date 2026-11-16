"""Throughput of guest `passt` against guest `routed`, interleaved.

Two paths, each both ways:

- forward: the host's iperf3 client to the guest's server, through the
  guest's forward (the egress's listener for `routed`).
- outbound: the guest's iperf3 client to a server on the host's
  127.0.0.1, which the guest reaches as 10.0.2.2 (the relay for `routed`).

ROUNDS rounds; each round measures both guests on one path and direction
before the next, so host load hits both alike.
"""

import json
import socket

import anyio

from vivarium_runner import Machine, Machines

DURATION = 5
ROUNDS = 2


def _rate(output: str) -> float:
    report = json.loads(output)
    return float(report["end"]["sum_received"]["bits_per_second"]) / 1e9


async def forward(iperf3: str, vm: Machine, reverse: bool) -> float:
    host, port = vm.reachable(5201)[0].rsplit(":", 1)
    argv = [iperf3, "--client", host, "--port", port, "--time", str(DURATION), "--json"]
    if reverse:
        argv.append("--reverse")
    with anyio.fail_after(DURATION + 30):
        result = await anyio.run_process(argv, check=False)
    if result.returncode != 0:
        raise AssertionError(f"{vm.name}: iperf3 failed:\n{result.stdout.decode()}{result.stderr.decode()}")
    return _rate(result.stdout.decode())


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def outbound(iperf3: str, vm: Machine, reverse: bool) -> float:
    port = _free_port()
    async with await anyio.open_process(
        [iperf3, "--server", "--bind", "127.0.0.1", "--port", str(port), "--one-off"]
    ) as server:
        await anyio.sleep(0.5)
        flag = " --reverse" if reverse else ""
        with anyio.fail_after(DURATION + 30):
            out = await vm.succeed(f"iperf3 --client 10.0.2.2 --port {port} --time {DURATION} --json{flag}")
        with anyio.fail_after(10):
            await server.wait()
    return _rate(out)


async def test(vms: Machines) -> None:
    iperf3 = vms.settings["iperf3"]
    for vm in (vms.passt, vms.routed):
        await vm.wait_for_unit("iperf3-server.service")
        mtu = (await vm.succeed("cat /sys/class/net/vec0/mtu")).strip()
        print(f"[test] {vm.name}: mtu {mtu}")
    for round_ in range(ROUNDS):
        for path, measure in (("forward", forward), ("outbound", outbound)):
            for reverse in (False, True):
                # forward: data host -> guest unless reversed; outbound: guest -> host unless reversed.
                if path == "forward":
                    direction = "guest -> host" if reverse else "host -> guest"
                else:
                    direction = "host -> guest" if reverse else "guest -> host"
                for vm in (vms.passt, vms.routed):
                    rate = await measure(iperf3, vm, reverse)
                    print(f"[test] round {round_} {path} {direction} {vm.name}: {rate:.2f} Gbit/s")
