"""`vivarium.recipes.cache`: a guest substitutes from the cache guest.

The cache guest registers `hello` and serves it. `client` does not have it
and lists only the cache guest as a substituter, so realising `hello` there
has to fetch it, and a path nobody holds has to fail at once rather than
wait on a network.
"""

import time

from vivarium_runner import MachineError, Machines

MISSING = "/nix/store/00000000000000000000000000000000-not-in-the-cache"


async def test(vms: Machines) -> None:
    cache, client = vms.cache, vms.client
    hello = vms.settings["hello"]

    # Socket-activated: the service starts on the first request.
    await cache.wait_for_unit("harmonia.socket")
    rc, _ = await client.execute(f"nix-store --check-validity {hello}")
    if rc == 0:
        raise MachineError(f"[client] {hello} is valid before any fetch")

    out = await client.succeed(f"nix-store --realise {hello} 2>&1")
    print(f"[test] client realised hello:\n{out}", flush=True)
    await client.succeed(f"{hello}/bin/hello")

    started = time.monotonic()
    rc, out = await client.execute(f"nix-store --realise {MISSING} 2>&1")
    took = time.monotonic() - started
    if rc == 0:
        raise MachineError(f"[client] {MISSING} realised: {out}")
    if took > 10:
        raise MachineError(f"[client] a miss took {took:.1f}s: {out}")
    print(f"[test] a miss fails in {took:.1f}s: {out.strip()}", flush=True)
