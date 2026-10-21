"""Two NixOS guests reach each other through RouterOS, one hop apart."""

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    r1, a, b = vms.r1, vms.a, vms.b

    names = await r1.succeed("/interface ethernet print terse")
    if "name=left " not in names or "name=right " not in names:
        raise AssertionError(f"the router's interfaces are not named by vivarium:\n{names}")
    print("[test] the router's interfaces carry their vivarium names")

    for peer in ("10.1.0.2", "10.2.0.2"):
        said = await r1.succeed(f"/ping {peer} count=3 interval=200ms")
        if "received=3" not in said.replace(" ", ""):
            raise AssertionError(f"r1 does not reach {peer}:\n{said}")
    print("[test] the router reaches both guests")

    for src, dst in ((a, "10.2.0.2"), (b, "10.1.0.2")):
        out = await src.succeed(f"ping -c 3 -W 2 {dst}")
        # A Linux guest answers with TTL 64, and the router takes one.
        if "ttl=63" not in out:
            raise AssertionError(f"{src.name} reached {dst} with no hop between:\n{out}")
    print("[test] a and b reach each other through one hop")
