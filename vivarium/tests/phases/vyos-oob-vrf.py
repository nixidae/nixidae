"""VyOS management VRF (nixidae/nixidae#958, #1523).

The VyOS analogue of oob-routeros. eth0, the uplink, leases from the
run's router in a `mgmt` VRF with a table of its own; the dataplane
(eth1) stays in the default table. The two tables are separate, so
management and dataplane cannot reach each other -- the isolation
sonic-vs could not instantiate, here with VyOS's real VRFs. The router's
lease gives the mgmt VRF a default route, so a ping from it to the
dataplane goes to the router and gets no reply.
"""

from __future__ import annotations

from vivarium_runner import Machine, MachineError, Machines

DATA_HOST = "10.9.0.2"
"""The host behind the dataplane interface."""
VRF = "mgmt"


async def _lease(vyos: Machine) -> str:
    """Wait for eth0 to hold its router address in the mgmt VRF; return it."""
    address = vyos.uplink_ip
    if address is None:
        raise AssertionError("vyos is not behind the router")
    converged = (
        f"ip -4 -brief addr show eth0 | grep -qw '{address}/24' && ip -d link show eth0 | grep -q 'master {VRF}'"
    )
    try:
        await vyos.wait_until_succeeds(converged, timeout=300)
    except MachineError as error:
        status = (await vyos.execute("systemctl --no-pager status vyos-router 2>&1")).stdout
        raise AssertionError(
            f"eth0 did not lease {address} in the {VRF} VRF\n{error}\nvyos-router:\n{status}"
        ) from None
    return address


async def test(vms: Machines) -> None:
    vyos = vms.vyos
    svc = vms.svc
    mgmt_host = svc.uplink_ip
    if mgmt_host is None:
        raise AssertionError("svc is not behind the router")

    address = await _lease(vyos)
    print(f"[test] eth0 leased {address} in the {VRF} VRF")

    # The reachable directions, once the lease and the data host settle.
    await vyos.wait_until_succeeds(f"ip vrf exec {VRF} ping -c2 -W2 {mgmt_host}", timeout=120)
    await vyos.wait_until_succeeds(f"ping -c2 -W2 {DATA_HOST}", timeout=120)
    print(f"[test] the {VRF} VRF reaches the mgmt host; the default table reaches the dataplane")

    # The isolated directions: separate tables.
    rc = (await vyos.execute(f"ip vrf exec {VRF} ping -c2 -W2 {DATA_HOST}")).returncode
    if rc == 0:
        raise AssertionError(f"the {VRF} VRF reached the dataplane; the tables are not isolated")
    rc = (await vyos.execute(f"ping -c2 -W2 {mgmt_host}")).returncode
    if rc == 0:
        raise AssertionError("the default table reached the mgmt host; the tables are not isolated")
    print(f"[test] the {VRF} VRF and the default table cannot reach each other")

    await svc.succeed(f"ping -c2 -W2 {address}")
    print("[test] the management host reaches VyOS's management through the router's switch")
