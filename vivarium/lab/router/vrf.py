#!/usr/bin/env agent-python
"""E11: one VRF per router network (nixidae/nixidae#1523).

Runs in an unprivileged user and network namespace on the host; the
namespace is the router. Each network is a VRF with a bridge, a guest
network namespace on a veth port, and its own relay bound to the VRF
(`RELAY_DEVICE`). One egress per network listens on a unix socket, as
a container router's does. A dummy `out0` (192.0.2.1) stands for the
outside, 127.0.0.1 for the host's loopback.

    unshare -rnm vrf.py run RELAY_BIN_DIR [--no-vrf]

`--no-vrf` is the control: the bridges stay in the main table and the
relays bind to no device.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import os
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

RUN = Path("/run/lab")
OUTSIDE = "192.0.2.1"
LOOPBACK = "127.0.0.1"


@dataclass(frozen=True, slots=True)
class Network:
    name: str
    index: int

    @property
    def vrf(self) -> str:
        return f"vrf-{self.name}"

    @property
    def bridge(self) -> str:
        return f"br-{self.name}"

    @property
    def table(self) -> int:
        return 10 * self.index

    @property
    def gateway(self) -> str:
        return f"10.{self.index}.0.1"

    @property
    def guest(self) -> str:
        return f"10.{self.index}.0.10"

    @property
    def tcp_port(self) -> int:
        return 9000 + 10 * self.index

    @property
    def udp_port(self) -> int:
        return 9001 + 10 * self.index

    def path(self, end: str) -> Path:
        return RUN / f"{self.name}-{end}.sock"


NETWORKS = (Network("a", 1), Network("b", 2))


def sh(*argv: str) -> None:
    subprocess.run(argv, check=True)


def ruleset(networks: tuple[Network, ...]) -> str:
    lines = ["table ip router {", "  chain prerouting {", "    type nat hook prerouting priority dstnat;"]
    for net in networks:
        onlink = f"{{ 10.{net.index}.0.0/24, 224.0.0.0/4, 255.255.255.255 }}"
        lines += [
            f'    iifname "{net.vrf}" counter comment "vrf-{net.name}-nat"',
            f'    iifname "{net.bridge}" counter comment "br-{net.name}-nat"',
            f'    iifname "{net.bridge}" ip daddr != {onlink} meta l4proto tcp redirect to :{net.tcp_port}',
            f'    iifname "{net.bridge}" ip daddr {net.gateway} meta l4proto tcp redirect to :{net.tcp_port}',
        ]
    lines += ["  }", "  chain mangle {", "    type filter hook prerouting priority mangle;"]
    # LAB_TPROXY_IIF=vrf: tproxy on the hook's second pass, which a VRF
    # runs with itself as the input device.
    on_vrf = os.environ.get("LAB_TPROXY_IIF") == "vrf"
    for net in networks:
        onlink = f"{{ 10.{net.index}.0.0/24, 224.0.0.0/4, 255.255.255.255 }}"
        iif = net.vrf if on_vrf else net.bridge
        lines += [
            f'    iifname "{net.vrf}" counter comment "vrf-{net.name}-mangle"',
            f'    iifname "{iif}" ip daddr != {onlink} meta l4proto udp meta mark set 1 tproxy to :{net.udp_port}',
            f'    iifname "{iif}" ip daddr {net.gateway} udp dport != 67 meta mark set 1 tproxy to :{net.udp_port}',
            f'    iifname "{net.bridge}" ip daddr != {onlink} icmp type echo-request meta mark set 1',
        ]
    lines += ["  }", "}"]
    return "\n".join(lines) + "\n"


def setup(bindir: Path, *, vrf: bool) -> list[subprocess.Popen[bytes]]:
    sh("mount", "-t", "tmpfs", "none", "/run")
    RUN.mkdir()
    sh("ip", "link", "set", "lo", "up")
    sh("sysctl", "-qw", "net.ipv4.ping_group_range=0 0")
    # As the router guest (modules/router.nix): no ip hooks for bridged frames.
    for family in ("iptables", "ip6tables", "arptables"):
        sh("sysctl", "-qw", f"net.bridge.bridge-nf-call-{family}=0")
    if os.environ.get("LAB_UNBOUND") == "1":
        for proto in ("tcp", "udp", "raw"):
            sh("sysctl", "-qw", f"net.ipv4.{proto}_l3mdev_accept=1")
    # The outside is a namespace of its own: a relay's per-flow UDP socket
    # binds the destination, which must not be local to the router.
    sh("ip", "netns", "add", "world")
    sh("ip", "link", "add", "out0", "type", "veth", "peer", "name", "eth0", "netns", "world")
    sh("ip", "address", "add", "192.0.2.254/24", "dev", "out0")
    sh("ip", "link", "set", "out0", "up")
    enter("world", "ip", "link", "set", "lo", "up")
    enter("world", "ip", "link", "set", "eth0", "up")
    enter("world", "ip", "address", "add", f"{OUTSIDE}/24", "dev", "eth0")
    enter("world", "sysctl", "-qw", "net.ipv4.icmp_echo_ignore_all=0")
    for net in NETWORKS:
        if vrf:
            sh("ip", "link", "add", net.vrf, "type", "vrf", "table", str(net.table))
            sh("ip", "link", "set", net.vrf, "up")
        sh("ip", "link", "add", net.bridge, "type", "bridge")
        if vrf:
            sh("ip", "link", "set", net.bridge, "master", net.vrf)
        sh("ip", "address", "add", f"{net.gateway}/24", "dev", net.bridge)
        sh("ip", "link", "set", net.bridge, "up")
        guest = f"g{net.name}"
        sh("ip", "netns", "add", guest)
        sh("ip", "link", "add", f"p{net.name}", "type", "veth", "peer", "name", "eth0", "netns", guest)
        sh("ip", "link", "set", f"p{net.name}", "master", net.bridge, "up")
        enter(guest, "ip", "link", "set", "lo", "up")
        enter(guest, "ip", "link", "set", "eth0", "up")
        enter(guest, "ip", "address", "add", f"{net.guest}/24", "dev", "eth0")
        enter(guest, "ip", "route", "add", "default", "via", net.gateway)
        enter(guest, "sysctl", "-qw", "net.ipv4.ping_group_range=0 0")
    # LAB_MARK: how a marked packet reaches a local route.
    mark = mark_mode()
    if mark == "rule500":
        sh("ip", "rule", "add", "fwmark", "1", "table", "100", "priority", "500")
        sh("ip", "route", "add", "local", "0.0.0.0/0", "dev", "lo", "table", "100")
    elif mark == "rule2000":
        sh("ip", "rule", "add", "fwmark", "1", "table", "100", "priority", "2000")
        sh("ip", "route", "add", "local", "0.0.0.0/0", "dev", "lo", "table", "100")
    elif mark == "vrfdev":
        sh("ip", "rule", "add", "fwmark", "1", "table", "100", "priority", "500")
        for net in NETWORKS:
            sh("ip", "route", "add", "local", "0.0.0.0/0", "dev", net.vrf, "table", "100")
    elif mark == "pervrf":
        for net in NETWORKS:
            sh("ip", "rule", "add", "fwmark", "1", "iif", net.vrf, "table", str(100 + net.index), "priority", "500")
            sh("ip", "route", "add", "local", "0.0.0.0/0", "dev", net.vrf, "table", str(100 + net.index))
    (RUN / "router.nft").write_text(ruleset(NETWORKS))
    sh("nft", "-f", str(RUN / "router.nft"))

    procs = [
        serve("world", OUTSIDE, "outside"),
        serve(None, LOOPBACK, "loopback"),
    ]
    for net in NETWORKS:
        procs.append(serve(f"g{net.name}", net.guest, f"guest-{net.name}"))
        # A host forward to each guest: 127.0.0.1:8100+index.
        host_forward = f"{8100 + net.index}={net.guest}:8000@{net.path('in')}"
        procs.append(subprocess.Popen([str(bindir / "egress"), str(net.path("egress")), net.gateway, host_forward]))
        # LAB_UNBOUND=1: VRFs, but relays bound to no device, accepting
        # through the l3mdev_accept sysctls instead.
        unbound = os.environ.get("LAB_UNBOUND") == "1"
        env = dict(os.environ, RELAY_DEVICE=net.vrf if vrf and not unbound else "")
        procs.append(
            subprocess.Popen(
                [
                    str(bindir / "relay"),
                    str(net.tcp_port),
                    f"unix:{net.path('egress')}",
                    str(net.udp_port),
                    f"unix:{net.path('in')}",
                ],
                env=env,
            )
        )
    time.sleep(1)
    return procs


def mark_mode() -> str:
    return os.environ.get("LAB_MARK", "rule500")


def enter(guest: str, *argv: str) -> None:
    sh("nsenter", f"--net=/run/netns/{guest}", *argv)


def serve(guest: str | None, address: str, tag: str) -> subprocess.Popen[bytes]:
    argv = [sys.executable, __file__, "serve", address, tag]
    if guest is not None:
        argv = ["nsenter", f"--net=/run/netns/{guest}", *argv]
    return subprocess.Popen(argv)


def serve_main(address: str, tag: str) -> None:
    tcp = socket.create_server((address, 8000))
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind((address, 8000))
    pid = os.fork()
    if pid == 0:
        while True:
            data, peer = udp.recvfrom(2048)
            udp.sendto(tag.encode() + b" " + data, peer)
    while True:
        conn, _ = tcp.accept()
        with conn:
            conn.sendall(tag.encode())


def probe(guest: str, kind: str, address: str) -> str:
    """Runs one probe from `guest`; its answer, or the error."""
    out = subprocess.run(
        ["nsenter", f"--net=/run/netns/{guest}", sys.executable, __file__, "probe", kind, address],
        capture_output=True,
        text=True,
        timeout=20,
    )
    lines = (out.stdout.strip() or out.stderr.strip()).splitlines()
    return lines[-1] if lines else f"rc={out.returncode}"


def probe_main(kind: str, target: str) -> None:
    address, _, port_text = target.partition(":")
    port = int(port_text or 8000)
    try:
        if kind == "tcp":
            with socket.create_connection((address, port), timeout=3) as conn:
                print(conn.recv(100).decode())
        elif kind == "udp":
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                udp.settimeout(3)
                udp.sendto(b"ping", (address, 8000))
                print(udp.recv(100).decode())
        elif kind == "icmp":
            out = subprocess.run(["ping", "-c1", "-W2", address], capture_output=True, text=True)
            print("reply" if out.returncode == 0 else "no reply")
    except OSError as err:
        print(f"error: {err.strerror or err}")


def forward(net: Network, address: str) -> str:
    """A host forward through `net`'s relay to `address`:8000."""
    ip = ipaddress.IPv4Address(address)
    request = bytes([6, 4]) + ip.packed + bytes(12) + struct.pack(">H", 8000)
    try:
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(3)
            conn.connect(str(net.path("in")))
            conn.sendall(request)
            status = conn.recv(1)
            if status != b"\0":
                return f"refused ({status!r})"
            return conn.recv(100).decode()
    except OSError as err:
        return f"error: {err.strerror or err}"


def counters() -> dict[str, int]:
    out = subprocess.run(
        ["nft", "-j", "list", "chain", "ip", "router", "prerouting"], capture_output=True, text=True, check=True
    )
    found: dict[str, int] = {}
    for item in json.loads(out.stdout)["nftables"]:
        rule = item.get("rule")
        if rule is None or "comment" not in rule:
            continue
        for expr in rule["expr"]:
            if "counter" in expr:
                found[rule["comment"]] = expr["counter"]["packets"]
    return found


def run_main(bindir: Path, *, vrf: bool) -> None:
    procs = setup(bindir, vrf=vrf)
    a, b = NETWORKS
    try:
        cases = [
            ("ga tcp outside", probe("ga", "tcp", OUTSIDE)),
            ("ga tcp gateway->loopback", probe("ga", "tcp", a.gateway)),
            ("ga udp outside", probe("ga", "udp", OUTSIDE)),
            ("ga icmp outside", probe("ga", "icmp", OUTSIDE)),
            ("gb tcp outside", probe("gb", "tcp", OUTSIDE)),
            ("gb udp outside", probe("gb", "udp", OUTSIDE)),
            ("forward a -> ga", forward(a, a.guest)),
            ("forward b -> gb", forward(b, b.guest)),
            ("LEAK? ga tcp gb", probe("ga", "tcp", b.guest)),
            ("LEAK? ga udp gb", probe("ga", "udp", b.guest)),
            ("LEAK? ga icmp gb", probe("ga", "icmp", b.guest)),
            ("LEAK? ga tcp router-b", probe("ga", "tcp", b.gateway)),
            ("LEAK? forward a -> gb", forward(a, b.guest)),
            ("ga tcp own host forward", probe("ga", "tcp", f"{a.gateway}:{8100 + a.index}")),
            ("LEAK? ga tcp b's host forward", probe("ga", "tcp", f"{a.gateway}:{8100 + b.index}")),
        ]
        stats = subprocess.run(["nstat", "-az"], capture_output=True, text=True, check=True).stdout
        nonzero = [
            line
            for line in stats.splitlines()[1:]
            if line.split()[1] != "0" and line.split()[0].startswith(("Udp", "Ip", "IpExt", "Icmp"))
        ]
        print(
            json.dumps(
                {"vrf": vrf, "mark": mark_mode(), "cases": dict(cases), "counters": counters(), "nstat": nonzero},
                indent=2,
            )
        )
    finally:
        for proc in procs:
            proc.kill()


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("bindir", type=Path)
    run.add_argument("--no-vrf", action="store_true")
    serve_cmd = sub.add_parser("serve")
    serve_cmd.add_argument("address")
    serve_cmd.add_argument("tag")
    probe_cmd = sub.add_parser("probe")
    probe_cmd.add_argument("kind")
    probe_cmd.add_argument("address")
    args = parser.parse_args()
    if args.command == "run":
        run_main(args.bindir, vrf=not args.no_vrf)
    elif args.command == "serve":
        with contextlib.suppress(KeyboardInterrupt):
            serve_main(args.address, args.tag)
    else:
        probe_main(args.kind, args.address)


if __name__ == "__main__":
    main()
