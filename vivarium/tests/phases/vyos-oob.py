"""VyOS on the out-of-band fabric (nixidae/nixidae#958).

VyOS's uplink (eth0) is on the management segment and leases from the
services guest's Kea. The vyos-guest module renames vec0 to eth0 and
DHCPs it; this proves VyOS reaches the fabric over a lease, not a static
address. The agent is on ttyS0, so it answers while eth0 is still coming
up.
"""

from vivarium_runner import Machine, MachineError, Machines

LEASE = r"ip -4 -brief addr show eth0 | grep -m1 -oE '10\.42\.0\.(1[0-4][0-9]|150)'"
"""Print eth0's address once it holds one from Kea's pool, 10.42.0.100-150."""


async def _lease(vyos: Machine) -> str:
    """eth0's pool address. vyos-router applies the config and starts the
    DHCP client well after the agent is up, so name the stage on a timeout."""
    try:
        return (await vyos.wait_until_succeeds(LEASE, timeout=300)).strip()
    except MachineError as error:
        _, status = await vyos.execute("systemctl --no-pager status vyos-router")
        raise AssertionError(f"{error}\nvyos-router:\n{status}") from None


async def test(vms: Machines) -> None:
    vyos = vms.vyos
    svc = vms.svc

    await svc.wait_for_unit("kea-dhcp4-server.service")
    release = await vyos.succeed(". /etc/os-release && echo $ID")
    print(f"[test] the agent answers from {release.strip()}")

    address = await _lease(vyos)
    print(f"[test] VyOS's eth0 leased {address} from Kea over the management segment")

    await vyos.succeed("ping -c1 -W2 10.42.0.1")
    await svc.succeed(f"ping -c1 -W2 {address}")
    print("[test] VyOS and the services guest reach each other over the fabric")

    leases = await svc.succeed("cat /var/lib/kea/dhcp4.leases")
    if address not in leases:
        raise AssertionError(f"Kea's lease file does not name {address}:\n{leases}")
    print("[test] Kea recorded the lease")
