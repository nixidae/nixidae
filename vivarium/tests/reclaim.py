#!/usr/bin/env python3
"""A guest gives cold page cache back to the host by itself.

`tests/memory.py` drops the guest's caches and watches free page reporting
return what they held. Nothing drops anything here: the guest fills its
page cache, goes idle, and DAMON_RECLAIM (`vivarium.reclaim`) has to find
the cache cold and reclaim it. Reporting then hands the pages back, and
what the host pays falls with no request from anyone.

The fill is from the guest's own disk, because a krun guest maps its store
from the host's page cache and reading it caches nothing in the guest.
"""

import anyio
from vivarium_runner import Machine, Machines

#: Random bytes, so no layer below can store them as zero pages.
FILL = "dd if=/dev/urandom of=/var/tmp/fill bs=1M count=400 status=none && sync && cat /var/tmp/fill > /dev/null"

#: How much page cache the fill has to produce, in kibibytes.
GREW_KIB = 300 * 1024

#: How much of what the fill cost the host has to come back by itself.
#: The same share `tests/memory.py` asks of an explicit drop.
SETTLED_SHARE = 2 / 3

#: The node's `vivarium.reclaim.minAge` is 10 s. DAMON needs a few
#: aggregation intervals to find the region cold, then pages it out at
#: 128 MiB a second at most, and reporting runs every two seconds.
RECLAIM_TIMEOUT = 120

PARAMETERS = "/sys/module/damon_reclaim/parameters"


async def damon(vm: Machine, name: str) -> str:
    return (await vm.succeed(f"cat {PARAMETERS}/{name}")).strip()


async def test(vms: Machines) -> None:
    vm = vms.node
    assert await damon(vm, "enabled") == "Y", "DAMON_RECLAIM is not enabled in the guest"

    booted = vm.host_memory_kib()
    before = (await vm.meminfo())["Cached"]
    await vm.succeed(FILL, timeout=300)
    cached = (await vm.meminfo())["Cached"]
    filled = vm.host_memory_kib()
    print(f"[test] after filling: guest cached {before // 1024}M -> {cached // 1024}M, host {booted // 1024}M -> {filled // 1024}M")
    assert cached - before > GREW_KIB, f"the fill cached {(cached - before) // 1024}M, so nothing after this measures anything"
    assert filled - booted > GREW_KIB, "the host did not start paying for that cache"

    target = filled - int((filled - booted) * SETTLED_SHARE)
    started = anyio.current_time()
    paying = filled
    with vm.waiting("DAMON_RECLAIM to reclaim the cold cache"):
        with anyio.move_on_after(RECLAIM_TIMEOUT):
            while (paying := vm.host_memory_kib()) > target:
                await anyio.sleep(1)
    elapsed = anyio.current_time() - started
    left = (await vm.meminfo())["Cached"]
    reclaimed = await damon(vm, "bytes_reclaimed_regions")
    print(
        f"[test] after {elapsed:.0f}s idle: guest cached {left // 1024}M, host {paying // 1024}M, "
        f"DAMON reclaimed {int(reclaimed) >> 20}M"
    )
    assert paying <= target, (
        f"the host still pays {paying // 1024}M after {RECLAIM_TIMEOUT}s, wanted {target // 1024}M: "
        "the guest kept its cold cache, or reclaimed it and did not report it"
    )
