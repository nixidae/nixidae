"""VyOS boots with the config Nix wrote for it.

The config addresses VyOS's first segment port, eth1, in both families;
the peer reaching those addresses says the config loaded and the ports
are numbered as `vivarium.appliance` promises: eth0 the uplink, then the
segments in `interfaces` order.

A firewall rule drops invalid ICMP. If a migration ran at boot, it took
the rule's `state` and the rule drops every ping: so a reply also says the
footer kept VyOS from migrating a config that is already current.
"""

import asyncio

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    for address in ("192.0.2.1", "2001:db8::1"):
        # The console says `configure.` before the commit has finished.
        for _ in range(60):
            rc, _ = await vms.peer.execute(f"ping -c 1 -W 2 {address}")
            if rc == 0:
                break
            await asyncio.sleep(2)
        else:
            raise AssertionError(f"VyOS never answered at {address}")
        print(f"[test] the peer reaches VyOS at {address}")
