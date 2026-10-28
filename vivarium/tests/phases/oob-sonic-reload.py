"""Push a whole config_db.json to SONiC and apply it with `config reload`.

The heavier provisioning path the `provision` phase names: instead of a
`config` command per setting, the services guest pushes a full
config_db.json and SONiC reloads from it (nixidae/nixidae#958). It pulls
SONiC's running config so the base matches the platform, edits it, pushes
it back, and reloads. `config reload` restarts SONiC's services but not
eth0 -- the management interface is outside config_db -- so SSH survives.
The pulled config is a scratch base, not a record of config management.
"""

import anyio
from vivarium_runner import Machines

OPTS = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5"
SSH = f"sshpass -p YourPaSsWoRd ssh {OPTS}"
SCP = f"sshpass -p YourPaSsWoRd scp {OPTS}"
LEASE = r"grep -m1 -oE '10\.42\.0\.(1[0-4][0-9]|150)' /var/lib/kea/dhcp4.leases"

LOOPBACK = "10.7.7.7"
HOSTNAME = "sonic-pushed"


async def test(vms: Machines) -> None:
    svc = vms.svc

    ip = (await svc.wait_until_succeeds(LEASE, timeout=60)).strip()
    print(f"[reload] SONiC at {ip}")

    # The switch stack must be up before a reload, or it refuses. The grep
    # runs on SONiC, so its exit status is the condition.
    await svc.wait_until_succeeds(
        f"{SSH} admin@{ip} 'show ip interfaces | grep -qF 10.1.0.1'", timeout=240
    )

    # Pull the running config (over scp, so no login banner lands in it),
    # edit it on the services guest, and push the whole file back.
    await svc.succeed(f"{SSH} admin@{ip} 'sudo sonic-cfggen -d --print-data | sudo tee /tmp/cfg.json >/dev/null'")
    await svc.succeed(f"{SCP} admin@{ip}:/tmp/cfg.json /tmp/cfg.json")
    await svc.succeed(
        "jq '.LOOPBACK_INTERFACE[\"Loopback0|" + LOOPBACK + "/32\"]={} |"
        f' .DEVICE_METADATA.localhost.hostname="{HOSTNAME}"\' /tmp/cfg.json > /tmp/new.json'
    )
    await svc.succeed(f"{SCP} /tmp/new.json admin@{ip}:/tmp/new.json")
    print("[reload] pushed a modified config_db.json to SONiC")

    # Full-file apply. It restarts SONiC's services, so give it room. SwSS
    # reports ready later than the dataplane shows the startup config, and a
    # reload before then is refused; a refused attempt bails at once, so
    # retry until SONiC accepts it.
    out = ""
    try:
        with anyio.fail_after(240):
            while True:
                rc, out = await svc.execute(
                    f"{SSH} admin@{ip} 'sudo config reload -y /tmp/new.json' 2>&1", timeout=300
                )
                if rc == 0:
                    break
                if "not ready" not in out:
                    raise AssertionError(f"config reload failed:\n{out[-400:]}")
                await anyio.sleep(5)
    except TimeoutError:
        raise AssertionError(f"SwSS never became ready for a reload:\n{out[-400:]}") from None
    print("[reload] config reload applied the whole file")

    # SSH still answers (mgmt is outside config_db), and both the
    # control-plane (hostname) and dataplane (loopback) changes took.
    await svc.wait_until_succeeds(f"{SSH} admin@{ip} 'hostname | grep -qF {HOSTNAME}'", timeout=120)
    await svc.wait_until_succeeds(
        f"{SSH} admin@{ip} 'show ip interfaces | grep -qF {LOOPBACK}'", timeout=120
    )
    print(f"[reload] SONiC came back as {HOSTNAME} with Loopback0 {LOOPBACK} from the pushed file")
