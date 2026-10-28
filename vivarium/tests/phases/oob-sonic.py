"""The services guest provisions SONiC over SSH, the real way.

SONiC's eth0 is its uplink on the management segment; it leases from the
services guest's Kea. The services guest finds that address, reaches SONiC
over SSH, and applies configuration (nixidae/nixidae#958). No console
beyond the boot login.
"""

from vivarium_runner import Machines

SSH = "sshpass -p YourPaSsWoRd ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5"
LEASE = r"grep -m1 -oE '10\.42\.0\.(1[0-4][0-9]|150)' /var/lib/kea/dhcp4.leases"
"""SONiC is the only client, so the one pool address in Kea's file is its."""


async def test(vms: Machines) -> None:
    svc = vms.svc

    await svc.wait_for_unit("kea-dhcp4-server.service")

    ip = (await svc.wait_until_succeeds(LEASE, timeout=180)).strip()
    print(f"[test] SONiC leased {ip} from the services guest's Kea")

    await svc.wait_until_succeeds(f"{SSH} admin@{ip} true", timeout=120)
    version = await svc.succeed(f"{SSH} admin@{ip} 'show version | head -3'")
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
