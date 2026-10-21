"""VyOS under UML, from its own squashfs, driven like any other guest.

The agent answers from VyOS's userspace, and VyOS applies the
`config.boot` the node gave it: the host name and a dummy interface.
"""

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    vyos = vms.vyos
    release = await vyos.succeed(". /etc/os-release && echo $ID")
    print(f"[test] the agent answers from {release.strip()}")

    # vyos-router applies config.boot after the agent is up.
    await vyos.succeed(
        "for i in $(seq 300); do ip -br addr show dum0 2>/dev/null | grep -q 10.255.0.1 && exit 0;"
        " sleep 1; done; exit 1",
        timeout=330,
    )
    name = await vyos.succeed("hostname")
    if name.strip() != "vyos1":
        raise AssertionError(f"the host name is {name!r}")
    print("[test] VyOS applied config.boot: dum0 has 10.255.0.1, the host name is vyos1")

    # vyos-router loads it before the config; it needs nftables' fib.
    await vyos.succeed("nft list chain ip6 raw vyos_rpfilter")
    print("[test] VyOS's firewall init loaded")

    version = await vyos.succeed("/opt/vyatta/bin/vyatta-op-cmd-wrapper show version")
    print(f"[test] show version:\n{version}")
