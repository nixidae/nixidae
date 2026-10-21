"""The services guest provisions SONiC over SSH, the real way.

SONiC's eth0 is its uplink on the management segment; it leases from the
services guest's Kea. The services guest finds that address, reaches SONiC
over SSH, and applies configuration (nixidae/nixidae#958). No console
beyond the boot login.
"""

import asyncio
import re

from vivarium_runner import Machine, Machines

POOL = re.compile(r"10\.42\.0\.(?:1[0-4][0-9]|150)")
SSH = (
    "sshpass -p YourPaSsWoRd ssh -o StrictHostKeyChecking=no"
    " -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5"
)


async def _poll(machine: Machine, command: str, want: str, timeout: float) -> str:
    """Run *command* on *machine* until *want* is in its output; the output."""
    deadline = asyncio.get_running_loop().time() + timeout
    out = ""
    while asyncio.get_running_loop().time() < deadline:
        _, out = await machine.execute(command, timeout=30)
        if want in out:
            return out
        await asyncio.sleep(3)
    raise AssertionError(f"`{command}` never produced {want!r}; last:\n{out}")


async def test(vms: Machines) -> None:
    svc = vms.svc

    await svc.wait_for_unit("kea-dhcp4-server.service")

    # SONiC is the only client on the segment, so its lease is the one in
    # Kea's file.
    leases = await _poll(svc, "cat /var/lib/kea/dhcp4.leases 2>/dev/null", "10.42.0.", 180)
    match = POOL.search(leases)
    if match is None:
        raise AssertionError(f"no pool address in Kea's lease file:\n{leases}")
    ip = match.group(0)
    print(f"[test] SONiC leased {ip} from the services guest's Kea")

    await _poll(svc, f"{SSH} admin@{ip} 'echo READY' 2>&1", "READY", 120)
    version = await svc.succeed(f"{SSH} admin@{ip} 'show version | head -3'")
    print(f"[test] the services guest reached SONiC over SSH:\n{version.strip()}")

    # Wait until SONiC's own startup config is applied -- the default
    # Loopback0 10.1.0.1 is in the dataplane view -- so the switch stack is
    # up and a provision now is not clobbered when SONiC loads its config.
    await _poll(svc, f"{SSH} admin@{ip} 'show ip interfaces' 2>&1", "10.1.0.1", 240)
    print("[test] SONiC's switch stack is up and its startup config applied")

    # Provision over SSH, the real way: `config` writes config_db. (A full
    # config_db.json replace via `config reload` is the heavier path.)
    await svc.succeed(f"{SSH} admin@{ip} 'sudo config interface ip add Loopback0 10.1.2.3/32'")
    await _poll(svc, f"{SSH} admin@{ip} 'show ip interfaces' 2>&1", "10.1.2.3/32", 60)
    print("[test] provisioned Loopback0 10.1.2.3/32 over SSH; SONiC shows it")
