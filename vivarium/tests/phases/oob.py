"""A guest's uplink leases a management address from a Kea services guest.

The out-of-band management fabric (nixidae/nixidae#958): one guest runs
Kea DHCP4 on a segment, another joins it as its uplink (`vec0`, not passt)
and leases there, and the two reach each other. This is how a foreign
guest's management interface reaches the fabric.
"""

from vivarium_runner import Machines

LEASE = r"ip -4 -brief addr show vec0 | grep -m1 -oE '10\.42\.0\.(1[0-4][0-9]|150)'"
"""Print vec0's address once it holds one from Kea's pool, 10.42.0.100-150.
`grep -m1` keeps the output one line and its exit status the match."""


async def test(vms: Machines) -> None:
    svc = vms.svc
    client = vms.client

    await svc.wait_for_unit("kea-dhcp4-server.service")
    print("[test] the services guest runs Kea DHCP4")

    # The client's uplink (vec0) leases an address from the pool.
    address = (await client.wait_until_succeeds(LEASE)).strip()
    print(f"[test] the client leased {address} from Kea")

    await client.succeed("ping -c1 -W2 10.42.0.1")
    await svc.succeed(f"ping -c1 -W2 {address}")
    print("[test] the client and the services guest reach each other over the management segment")

    leases = await svc.succeed("cat /var/lib/kea/dhcp4.leases")
    if address not in leases:
        raise AssertionError(f"Kea's lease file does not name {address}:\n{leases}")
    print("[test] Kea recorded the lease")
