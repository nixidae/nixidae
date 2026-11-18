"""The services guest provisions SONiC over SSH, the real way.

SONiC's eth0 is its uplink; it leases its address from the run's router,
the management switch (nixidae/nixidae#1523). The services guest, on the
same switch, reaches SONiC over SSH at that address and applies
configuration (nixidae/nixidae#958). No console beyond the boot login.
"""

from __future__ import annotations

from vivarium_runner import Machines

SSH = "sshpass -p YourPaSsWoRd ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5"


async def test(vms: Machines) -> None:
    svc, sw1 = vms.svc, vms.sw1
    ip = sw1.uplink_ip
    if ip is None:
        raise AssertionError("sw1 is not behind the router")

    await sw1.wait_until_succeeds(f"ip -4 -brief addr show eth0 | grep -qw {ip}/24", timeout=180)
    print(f"[test] SONiC leased {ip} on eth0 from the router")

    await svc.wait_until_succeeds(f"{SSH} admin@{ip} true", timeout=120)
    version = (await svc.succeed(f"{SSH} admin@{ip} 'show version | head -3'")).stdout
    print(f"[test] the services guest reached SONiC over SSH:\n{version.strip()}")

    # Wait until SONiC's own startup config is applied -- the default
    # Loopback0 10.1.0.1 is in the dataplane view -- so the switch stack is
    # up and a provision now is not clobbered when SONiC loads its config.
    # The grep runs on SONiC, so its exit status is the condition.
    await svc.wait_until_succeeds(f"{SSH} admin@{ip} 'show ip interfaces | grep -qF 10.1.0.1'", timeout=240)
    print("[test] SONiC's switch stack is up and its startup config applied")

    # Provision over SSH, the real way: `config` writes config_db. (A full
    # config_db.json replace via `config reload` is the oob-sonic-reload path.)
    await svc.succeed(f"{SSH} admin@{ip} 'sudo config interface ip add Loopback0 10.1.2.3/32'")
    await svc.wait_until_succeeds(f"{SSH} admin@{ip} 'show ip interfaces | grep -qF 10.1.2.3/32'", timeout=60)
    print("[test] provisioned Loopback0 10.1.2.3/32 over SSH; SONiC shows it")
