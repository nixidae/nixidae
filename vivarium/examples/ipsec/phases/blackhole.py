"""An ICMP black hole in the underlay, and MSS clamping as the fix.

west drops every outgoing ESP packet longer than 1400 bytes and sends no
ICMP, as a narrower hop with a filtered ICMP would. carol and dave do
not know: their path MTU is the XFRM interface's 1400, so TCP sends
1360-byte segments, each 1456 bytes once encrypted.

- Pings and the TCP handshake still pass; a 4 MB transfer stalls.
- Clamping the MSS to the route MTU (`rt mtu`, 1360 here) does not help:
  the route does not know about the narrower hop.
- Clamping to 1300 does: a 1340-byte inner packet is 1396 bytes as ESP.
"""

import re

import anyio
from lab import capture, lab, ping
from vivarium_runner import Machine, Machines

PORT = 5001
NARROW = 1400


async def transfer(carol: Machine, dave: str) -> int:
    rc, _ = await carol.execute(
        f"head -c 4000000 /dev/zero | timeout 15 socat -u STDIN TCP:{dave}:{PORT},connect-timeout=5",
        timeout=30,
    )
    return rc


async def clamp(west: Machine, value: str) -> None:
    await west.execute("nft delete table inet clamp")
    await west.succeed(
        "nft add table inet clamp; "
        "nft add chain inet clamp forward '{ type filter hook forward priority mangle; }'; "
        f"nft add rule inet clamp forward 'tcp flags & (syn | rst) == syn tcp option maxseg size set {value}'"
    )


async def syn_mss(dave: Machine, carol: Machine, address: str) -> int:
    """The MSS in carol's SYN as dave receives it."""
    seen: dict[str, tuple[int, str]] = {}

    async def watch() -> None:
        seen["syn"] = await capture(dave, "lan", f"tcp dst port {PORT} and tcp[tcpflags] & tcp-syn != 0", 1, 10, "-v")

    async with anyio.create_task_group() as tg:
        tg.start_soon(watch)
        await anyio.sleep(2)
        await carol.execute(f"echo | timeout 5 socat -u STDIN TCP:{address}:{PORT}")
    match = re.search(r"mss (\d+)", seen["syn"][1])
    if not match:
        raise AssertionError(f"no SYN with an MSS reached dave:\n{seen['syn'][1]}")
    return int(match.group(1))


async def test(vms: Machines) -> None:
    cfg = lab(vms)
    west, carol, dave = vms.west, vms.carol, vms.dave
    address = cfg["hosts"]["dave"]["address"]

    await dave.succeed(f"systemd-run --unit tcp-sink socat -u TCP-LISTEN:{PORT},reuseaddr,fork OPEN:/dev/null")
    try:
        if await transfer(carol, address) != 0:
            raise AssertionError("4 MB carol -> dave failed before any narrowing")
        print("[test] baseline: 4 MB carol -> dave through the tunnel")

        await west.succeed(
            "nft add table inet narrow; "
            "nft add chain inet narrow postrouting '{ type filter hook postrouting priority 10; }'; "
            f"nft add rule inet narrow postrouting oifname internet meta l4proto esp meta length gt {NARROW} counter drop"
        )
        await carol.succeed(ping(address))
        if await transfer(carol, address) == 0:
            raise AssertionError("the transfer passed a hop that drops ESP over 1400 bytes")
        dropped = await west.succeed("nft list chain inet narrow postrouting | grep -o 'packets [0-9]*'")
        print(f"[test] black hole: ping passes, the 4 MB transfer stalls; west dropped {dropped.split()[-1]} ESP packets over {NARROW} bytes")

        await clamp(west, "rt mtu")
        mss = await syn_mss(dave, carol, address)
        if mss != cfg["tunnelMtu"] - 40:
            raise AssertionError(f"rt mtu clamp: dave sees MSS {mss}, want {cfg['tunnelMtu'] - 40}")
        if await transfer(carol, address) == 0:
            raise AssertionError("clamping to the route MTU should not get past the narrower hop")
        print(f"[test] clamp to rt mtu: dave sees MSS {mss}, and the transfer still stalls")

        await clamp(west, "1300")
        mss = await syn_mss(dave, carol, address)
        if mss != 1300:
            raise AssertionError(f"dave sees MSS {mss}, want 1300")
        if await transfer(carol, address) != 0:
            raise AssertionError("the transfer still stalls with MSS 1300")
        print("[test] clamp to 1300: dave sees MSS 1300, 1340-byte packets are 1396 as ESP, 4 MB pass")
    finally:
        await west.execute("nft delete table inet narrow; nft delete table inet clamp")
        await dave.execute("systemctl stop tcp-sink")
