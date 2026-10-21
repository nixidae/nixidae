"""A NixOS services guest hands out a management lease with Kea.

The first piece of the out-of-band management fabric (nixidae/nixidae#958):
one guest runs Kea DHCP4 on a segment, another leases an address there and
the two reach each other. No foreign guest yet; this proves the services
guest.
"""

import asyncio
import re

from vivarium_runner import Machines

POOL = re.compile(r"10\.42\.0\.(1[0-4][0-9]|150)")
"""An address from Kea's pool, 10.42.0.100-150."""


async def test(vms: Machines) -> None:
    svc = vms.svc
    client = vms.client

    await svc.wait_for_unit("kea-dhcp4-server.service")
    print("[test] the services guest runs Kea DHCP4")

    # The client's mgmt interface leases an address from the pool. No
    # generic "retry a command" helper, so poll as `wait_for_unit` does.
    deadline = asyncio.get_running_loop().time() + 60
    address: str | None = None
    while asyncio.get_running_loop().time() < deadline:
        rc, out = await client.execute("ip -4 -brief addr show mgmt")
        match = POOL.search(out)
        if rc == 0 and match is not None:
            address = match.group(0)
            break
        await asyncio.sleep(1)
    if address is None:
        raise AssertionError("the client leased no pool address on mgmt within 60s")
    print(f"[test] the client leased {address} from Kea")

    await client.succeed("ping -c1 -W2 10.42.0.1")
    await svc.succeed(f"ping -c1 -W2 {address}")
    print("[test] the client and the services guest reach each other over the management segment")

    leases = await svc.succeed("cat /var/lib/kea/dhcp4.leases")
    if address not in leases:
        raise AssertionError(f"Kea's lease file does not name {address}:\n{leases}")
    print("[test] Kea recorded the lease")
