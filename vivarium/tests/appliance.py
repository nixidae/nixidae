"""An appliance guest: a vendor image, reached over the network only.

RouterOS CHR here, beside a NixOS guest on one segment. What is checked is
what an appliance can get wrong and a NixOS guest cannot:

  * the uplink is the first NIC, so passt's forward reaches the
    appliance's own management port;
  * each segment is one more NIC, in `interfaces` order, with the MAC
    vivarium chose, so the peer reaches it by IPv6 link-local address
    without either side being configured.
"""

import asyncio
import json
import urllib.request

from vivarium_runner import Machines

def link_local(mac: str) -> str:
    """The EUI-64 IPv6 link-local address RouterOS derives from *mac*."""
    octets = [int(part, 16) for part in mac.split(":")]
    octets[0] ^= 0x02
    eui = octets[:3] + [0xFF, 0xFE] + octets[3:]
    groups = [f"{eui[i] << 8 | eui[i + 1]:x}" for i in range(0, 8, 2)]
    return "fe80::" + ":".join(groups)


async def rest(where: str, path: str) -> object:
    """GET a RouterOS REST path as admin with the empty first-boot password."""
    request = urllib.request.Request(
        f"http://{where}/rest{path}",
        headers={"Authorization": "Basic YWRtaW46"},  # admin:
    )
    deadline = asyncio.get_running_loop().time() + 60
    while True:
        try:
            body = await asyncio.to_thread(
                lambda: urllib.request.urlopen(request, timeout=5).read()
            )
            return json.loads(body)
        except OSError as error:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"no REST answer on {where}: {error}")
            await asyncio.sleep(1)


async def test(vms: Machines) -> None:
    router, peer = vms.router, vms.peer

    where = router.reachable(80)
    assert where, "port 80 is not forwarded to the appliance"
    identity = await rest(where[0], "/system/identity")
    print(f"[test] REST through the uplink forward: {identity}")

    ports = await rest(where[0], "/interface/ethernet")
    assert isinstance(ports, list)
    macs = {port["name"]: port["mac-address"].lower() for port in ports}
    print(f"[test] the appliance's ports: {macs}")
    assert macs.get("ether1") == router.spec.mac(0), macs
    fabric = router.spec.interfaces[0].mac
    assert macs.get("ether2") == fabric, macs

    target = f"{link_local(fabric)}%fabric"
    await peer.succeed(f"ping -6 -c 3 -W 2 {target}")
    print(f"[test] the peer reaches {target} across the segment")
