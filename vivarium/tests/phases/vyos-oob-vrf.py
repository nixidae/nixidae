"""VyOS management VRF on the out-of-band fabric (nixidae/nixidae#958).

The VyOS analogue of oob-routeros. eth0, the uplink, leases in a `mgmt`
VRF with a table of its own; the dataplane (eth1) stays in the default
table. The two tables are separate, so management and dataplane cannot
reach each other -- the isolation sonic-vs could not instantiate, here
with VyOS's real VRFs.
"""

from vivarium_runner import Machine, MachineError, Machines

MGMT_HOST = "10.42.0.1"
"""The services guest, on the management segment."""
DATA_HOST = "10.9.0.2"
"""The host behind the dataplane interface."""
VRF = "mgmt"

POOL = r"10\.42\.0\.(1[0-4][0-9]|150)"
CONVERGED = (
    rf"ip -4 -brief addr show eth0 | grep -qE '{POOL}'"
    f" && ip -d link show eth0 | grep -q 'master {VRF}'"
)
"""eth0 holds a pool address and is enslaved to the mgmt VRF."""
LEASE = rf"ip -4 -brief addr show eth0 | grep -m1 -oE '{POOL}'"


async def _lease(vyos: Machine) -> str:
    """Wait for eth0 to lease in the mgmt VRF; return its address."""
    try:
        await vyos.wait_until_succeeds(CONVERGED, timeout=300)
    except MachineError as error:
        _, status = await vyos.execute("systemctl --no-pager status vyos-router")
        raise AssertionError(f"eth0 did not lease in the {VRF} VRF\n{error}\nvyos-router:\n{status}") from None
    return (await vyos.succeed(LEASE)).strip()


async def test(vms: Machines) -> None:
    vyos = vms.vyos
    svc = vms.svc

    await svc.wait_for_unit("kea-dhcp4-server.service")

    address = await _lease(vyos)
    print(f"[test] eth0 leased {address} in the {VRF} VRF")

    # The reachable directions, once the lease and the data host settle.
    await vyos.wait_until_succeeds(f"ip vrf exec {VRF} ping -c2 -W2 {MGMT_HOST}", timeout=120)
    await vyos.wait_until_succeeds(f"ping -c2 -W2 {DATA_HOST}", timeout=120)
    print(f"[test] the {VRF} VRF reaches the mgmt host; the default table reaches the dataplane")

    # The isolated directions: separate tables, so no route.
    rc, _ = await vyos.execute(f"ip vrf exec {VRF} ping -c2 -W2 {DATA_HOST}")
    if rc == 0:
        raise AssertionError(f"the {VRF} VRF reached the dataplane; the tables are not isolated")
    rc, _ = await vyos.execute(f"ping -c2 -W2 {MGMT_HOST}")
    if rc == 0:
        raise AssertionError("the default table reached the mgmt host; the tables are not isolated")
    print(f"[test] the {VRF} VRF and the default table cannot reach each other")

    await svc.succeed(f"ping -c2 -W2 {address}")
    print("[test] the services guest reaches VyOS's management over the OOB segment")
