"""E10: what a NixOS guest costs as the router: host memory, boot, closure."""

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    for vm in vms.values():
        await vm.succeed("sync; echo 3 > /proc/sys/vm/drop_caches")
        print(f"[test] {vm.name} host_kib={vm.host_memory_kib()}")
        print(f"[test] {vm.name} analyze: {await vm.succeed('systemd-analyze 2>&1 | head -1')}")
        print(f"[test] {vm.name} closure: {await vm.succeed('du -shL /run/current-system/sw 2>/dev/null | cut -f1; nix-store -qR /run/current-system 2>/dev/null | wc -l')}")
        print(f"[test] {vm.name} units: {await vm.succeed('systemctl list-units --state=running --no-legend | wc -l')}")
        print(f"[test] {vm.name} used: {await vm.succeed('free -m | sed -n 2p')}")
        print(f"[test] {vm.name} blame:\n{await vm.succeed('systemd-analyze blame 2>&1 | head -12')}")
        print(f"[test] {vm.name} chain:\n{await vm.succeed('systemd-analyze critical-chain 2>&1 | head -25')}")
        print(f"[test] {vm.name} rss:\n{await vm.succeed('ps -eo rss,comm --sort=-rss | head -12')}")
