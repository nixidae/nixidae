"""VyOS management VRF on the out-of-band fabric (nixidae/nixidae#958).

The VyOS analogue of oob-routeros. eth0, the uplink, leases in a `mgmt`
VRF with a table of its own; the dataplane (eth1) stays in the default
table. The two tables are separate, so management and dataplane cannot
reach each other -- the isolation sonic-vs could not instantiate, here
with VyOS's real VRFs.
"""

import asyncio
import re

from vivarium_runner import Machine, Machines

MGMT_HOST = "10.42.0.1"
"""The services guest, on the management segment."""
DATA_HOST = "10.9.0.2"
"""The host behind the dataplane interface."""
VRF = "mgmt"
POOL = re.compile(r"10\.42\.0\.(1[0-4][0-9]|150)")


async def _reaches(vyos: Machine, dst: str, vrf: str | None = None) -> bool:
    """Does vyos reach *dst*, routed in *vrf* (default table otherwise)?"""
    prefix = f"ip vrf exec {vrf} " if vrf is not None else ""
    rc, _ = await vyos.execute(f"{prefix}ping -c2 -W2 {dst}")
    return rc == 0


async def _wait_reach(vyos: Machine, dst: str, vrf: str | None, timeout: float) -> None:
    """Wait until vyos reaches *dst* in *vrf*; the lease and the peer settle."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await _reaches(vyos, dst, vrf):
            return
        await asyncio.sleep(2)
    raise AssertionError(f"{dst} (vrf={vrf or 'default'}) never became reachable")


async def _lease(vyos: Machine, timeout: float) -> str:
    """Wait for eth0, in the mgmt VRF, to hold a pool address."""
    deadline = asyncio.get_running_loop().time() + timeout
    rc, addr, master = 1, "", ""
    while asyncio.get_running_loop().time() < deadline:
        rc, addr = await vyos.execute("ip -4 -brief addr show eth0")
        _, master = await vyos.execute("ip -d link show eth0")
        match = POOL.search(addr)
        if rc == 0 and match is not None and f"master {VRF}" in master:
            return match.group(0)
        await asyncio.sleep(2)
    _, router = await vyos.execute("systemctl --no-pager status vyos-router")
    raise AssertionError(
        f"eth0 did not lease in the {VRF} VRF within {timeout:.0f}s "
        f"(ip rc={rc}):\naddr: {addr}\nlink: {master}\nvyos-router:\n{router}"
    )


async def test(vms: Machines) -> None:
    vyos = vms.vyos
    svc = vms.svc

    await svc.wait_for_unit("kea-dhcp4-server.service")

    address = await _lease(vyos, 300)
    print(f"[test] eth0 leased {address} in the {VRF} VRF")

    # The reachable directions, once the lease and the data host settle.
    await _wait_reach(vyos, MGMT_HOST, VRF, 120)
    await _wait_reach(vyos, DATA_HOST, None, 120)
    print(f"[test] the {VRF} VRF reaches the mgmt host; the default table reaches the dataplane")

    # The isolated directions: separate tables, so no route.
    if await _reaches(vyos, DATA_HOST, VRF):
        raise AssertionError(f"the {VRF} VRF reached the dataplane; the tables are not isolated")
    if await _reaches(vyos, MGMT_HOST, None):
        raise AssertionError("the default table reached the mgmt host; the tables are not isolated")
    print(f"[test] the {VRF} VRF and the default table cannot reach each other")

    await svc.succeed(f"ping -c2 -W2 {address}")
    print("[test] the services guest reaches VyOS's management over the OOB segment")
