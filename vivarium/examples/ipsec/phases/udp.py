"""UDP has no MSS: it gets through by fragmentation or not at all.

- Without DF, a 1600-byte datagram is fragmented on the way (it does not
  fit carol's own 1500 link, let alone the tunnel) and dave reassembles
  all 1600 bytes.
- With DF (IP_PMTUDISC_DO, what QUIC and careful applications set), a
  1450-byte datagram leaves carol, west refuses it with ICMP, and it is
  lost: the application never learns unless it sends again. carol's
  kernel now caches mtu 1400, and the next send fails at once with
  EMSGSIZE ("Message too long"). Retrying smaller is the application's job.
"""

import anyio
from lab import eventually, lab, route_mtu
from vivarium_runner import Machine, Machines

PORT = 5002
SINK = "/tmp/udp.bin"


async def received(dave: Machine) -> int:
    return int(await dave.succeed(f"stat -c %s {SINK} 2>/dev/null || echo 0"))


async def send(carol: Machine, address: str, size: int, options: str = "") -> tuple[int, str]:
    return await carol.execute(
        f"head -c {size} /dev/urandom > /tmp/datagram && "
        f"socat -u -b 65536 OPEN:/tmp/datagram UDP-SENDTO:{address}:{PORT}{options} 2>&1"
    )


async def test(vms: Machines) -> None:
    cfg = lab(vms)
    carol, dave = vms.carol, vms.dave
    address = cfg["hosts"]["dave"]["address"]

    await dave.succeed(f"rm -f {SINK}; systemd-run --unit udp-sink socat -u -b 65536 UDP-RECV:{PORT} OPEN:{SINK},creat,append")
    try:
        await anyio.sleep(1)
        rc, out = await send(carol, address, 1600)
        if rc != 0:
            raise AssertionError(f"sending 1600 bytes without DF failed:\n{out}")
        await eventually(dave, f"test $(stat -c %s {SINK}) -eq 1600", 10)
        print("[test] no DF: a 1600-byte datagram is fragmented, and dave reassembles all 1600 bytes")

        await carol.succeed("ip route flush cache")
        rc, out = await send(carol, address, 1450, ",ip-mtu-discover=2")
        if rc != 0:
            raise AssertionError(f"the first DF send should leave carol; it failed locally:\n{out}")
        await anyio.sleep(3)
        if await received(dave) != 1600:
            raise AssertionError("the 1450-byte DF datagram reached dave through a 1400 tunnel")
        cached = await route_mtu(carol, address)
        if cached != cfg["tunnelMtu"]:
            raise AssertionError(f"carol's cached path MTU to dave is {cached}, want {cfg['tunnelMtu']}")
        print(f"[test] DF set: the 1450-byte datagram is lost at west, and carol now caches mtu {cached}")

        rc, out = await send(carol, address, 1450, ",ip-mtu-discover=2")
        if rc == 0 or "Message too long" not in out:
            raise AssertionError(f"the second DF send should fail with EMSGSIZE:\n{out}")
        print("[test] next DF send: EMSGSIZE at carol, nothing on the wire; resizing is the application's job")
    finally:
        await dave.execute("systemctl stop udp-sink")
