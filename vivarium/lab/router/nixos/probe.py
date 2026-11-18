"""E10: what a NixOS guest costs as the router: host memory, boot, closure."""

from __future__ import annotations

from vivarium_runner import Machines

PEERS = {"plain": "10.9.0.2", "slimmer": "10.9.0.1"}


async def test(vms: Machines) -> None:
    for vm in vms.values():
        await vm.succeed("sync; echo 3 > /proc/sys/vm/drop_caches")
        print(f"[test] {vm.name} host_kib={vm.host_memory_kib()}")
        print(f"[test] {vm.name} analyze: {(await vm.succeed('systemd-analyze 2>&1 | head -1')).stdout}")
        print(
            f"[test] {vm.name} closure: {(await vm.succeed('du -shL /run/current-system/sw 2>/dev/null | cut -f1; nix-store -qR /run/current-system 2>/dev/null | wc -l')).stdout}"
        )
        print(
            f"[test] {vm.name} units: {(await vm.succeed('systemctl list-units --state=running --no-legend | wc -l')).stdout}"
        )
        print(f"[test] {vm.name} used: {(await vm.succeed('free -m | sed -n 2p')).stdout}")
        print(f"[test] {vm.name} blame:\n{(await vm.succeed('systemd-analyze blame 2>&1 | head -12')).stdout}")
        print(f"[test] {vm.name} chain:\n{(await vm.succeed('systemd-analyze critical-chain 2>&1 | head -25')).stdout}")
        if vm.name in PEERS:
            rc = (await vm.execute(f"ping -c2 -W2 {PEERS[vm.name]}")).returncode
            print(f"[test] {vm.name} ping {PEERS[vm.name]}: rc={rc}")
        print(f"[test] {vm.name} links:\n{(await vm.succeed('ip -br addr; networkctl --no-pager 2>&1 | head')).stdout}")
        print(f"[test] {vm.name} rss:\n{(await vm.succeed('ps -eo rss,comm --sort=-rss | head -12')).stdout}")
