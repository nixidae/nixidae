"""RouterOS management VRF on the out-of-band fabric (nixidae/nixidae#958).

ether1, the uplink, is on the management segment and leases from the
services guest's Kea in a `mgmt` VRF; the dataplane interface stays in
`main`. The two routing tables are separate, so management and dataplane
cannot reach each other -- the model sonic-vs could not instantiate.

RouterOS is a device CLI, not a shell: `/ping` to an unreachable host
exits 0 and says "no route" in its output, so these poll the output
rather than an exit status, which is why they are not `wait_until_succeeds`.
"""

import anyio

from vivarium_runner import Machine, Machines

MGMT_HOST = "10.42.0.1"
"""The services guest, on the management segment."""
DATA_HOST = "10.9.0.2"
"""The host behind the dataplane interface."""


async def _reaches(r1: Machine, dst: str, vrf: str | None = None) -> bool:
    """Does r1 reach *dst*, routed in *vrf* (default `main`)?"""
    cmd = f"/ping {dst} count=2" + (f" vrf={vrf}" if vrf is not None else "")
    _, out = await r1.execute(cmd)
    return "no route" not in out and dst in out


async def _wait_reach(r1: Machine, dst: str, vrf: str | None, timeout: float) -> None:
    """Wait until r1 reaches *dst* in *vrf*; the lease and the peer settle."""
    try:
        with anyio.fail_after(timeout):
            while not await _reaches(r1, dst, vrf):
                await anyio.sleep(2)
    except TimeoutError:
        raise AssertionError(f"{dst} (vrf={vrf or 'main'}) never became reachable") from None


async def test(vms: Machines) -> None:
    r1 = vms.r1
    svc = vms.svc

    vrf = await r1.succeed("/ip vrf print terse")
    if "name=mgmt" not in vrf or "ether1" not in vrf:
        raise AssertionError(f"ether1 is not in a mgmt VRF:\n{vrf}")

    # ether1's Kea lease binds in the mgmt VRF; it may not be up yet.
    lease = ""
    try:
        with anyio.fail_after(60):
            while True:
                addresses = await r1.succeed("/ip address print terse")
                lease = next(
                    (line for line in addresses.splitlines() if "interface=ether1" in line), ""
                )
                if "vrf=mgmt" in lease and "10.42.0." in lease:
                    break
                await anyio.sleep(2)
    except TimeoutError:
        raise AssertionError(f"ether1 has no Kea lease in the mgmt VRF:\n{lease}") from None
    print(f"[test] ether1 leased in the mgmt VRF: {lease.split('address=')[1].split()[0]}")

    # The reachable directions, once the lease and the data host settle.
    await _wait_reach(r1, MGMT_HOST, "mgmt", 60)
    await _wait_reach(r1, DATA_HOST, None, 60)
    print("[test] the mgmt VRF reaches the mgmt host; main reaches the dataplane")

    # The isolated directions: separate routing tables, so no route.
    if await _reaches(r1, DATA_HOST, "mgmt"):
        raise AssertionError("the mgmt VRF reached the dataplane; the VRFs are not isolated")
    if await _reaches(r1, MGMT_HOST, None):
        raise AssertionError("the main VRF reached the mgmt network; the VRFs are not isolated")
    print("[test] the mgmt and main VRFs cannot reach each other")

    await svc.succeed("ping -c2 -W2 10.42.0.100")
    print("[test] the services guest reaches RouterOS's management over the OOB segment")
