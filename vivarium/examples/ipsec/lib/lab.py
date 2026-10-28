"""What every phase of the IPsec lab asks of the guests."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import anyio
from vivarium_runner import Machine, Machines

T = TypeVar("T")

CONVERGE = 60.0
"""Seconds for IKE, BGP or DPD to settle after boot or a change."""


def esp_inner_mtu(outer: int) -> int:
    """The largest inner packet a tunnel-mode AES-GCM SA carries in *outer* bytes.

    Linux's xfrm_state_mtu(): ((mtu - header_len - icv) & ~(align - 1)) - 2,
    with header_len = 20 (outer IPv4) + 8 (ESP header) + 8 (GCM IV), icv = 16,
    align = 4 (GCM's block size of 1, rounded up to ESP's 4) and 2 for the
    pad-length and next-header bytes. 1500 gives 1446, the maximum MTU AWS
    publishes for Site-to-Site VPN.
    """
    header_len = 20 + 8 + 8
    icv = 16
    align = 4
    return ((outer - header_len - icv) & ~(align - 1)) - 2


def lab(vms: Machines) -> dict[str, Any]:
    """The lab's topology, as Nix declared it; its keys are read where used."""
    value = vms.settings["lab"]
    if not isinstance(value, dict):
        raise AssertionError(f"settings.lab is {type(value).__name__}, not an object")
    return value


async def eventually(vm: Machine, command: str, timeout: float = CONVERGE) -> str:
    deadline = time.monotonic() + timeout
    while True:
        rc, out = await vm.execute(command)
        if rc == 0:
            return out
        if time.monotonic() > deadline:
            raise AssertionError(f"{vm.name}: still failing after {timeout:.0f}s: {command}\n{out}")
        await anyio.sleep(1)


async def until[T](vm: Machine, what: str, check: Callable[[], Awaitable[T | None]], timeout: float = CONVERGE) -> T:
    """Poll the async *check* until it returns something truthy."""
    deadline = time.monotonic() + timeout
    with vm.waiting(what):
        while True:
            last = await check()
            if last:
                return last
            if time.monotonic() > deadline:
                raise AssertionError(f"{vm.name}: {what} not reached in {timeout:.0f}s")
            await anyio.sleep(0.5)


def ping(address: str, count: int = 3, extra: str = "") -> str:
    return f"ping -n -c {count} -i 0.2 -W 2 {extra} {address}"


def parse_pretty(text: str) -> dict[str, Any]:
    """`swanctl --list-* --pretty`: `k = v`, `name {`, `k = [`, items, `}`/`]`.

    Each top-level `<event> {` block is merged into one dict, so several
    `list-sa event` blocks become one mapping of IKE_SA name to its SA.
    """
    root: dict[str, Any] = {}
    stack: list[Any] = [root]
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        top = stack[-1]
        if line in ("}", "]"):
            stack.pop()
        elif line.endswith(" {"):
            stack.append(top.setdefault(line[:-2], {}))
        elif line.endswith("["):
            items: list[str] = []
            top[line.removesuffix("[").removesuffix(" = ").strip()] = items
            stack.append(items)
        elif isinstance(top, list):
            top.append(line)
        elif " = " in line:
            key, value = line.split(" = ", 1)
            top[key] = value
    return root


async def sas(vm: Machine) -> dict[str, dict[str, Any]]:
    """The IKE_SAs charon holds, by connection name."""
    out = await vm.succeed("swanctl --list-sas --pretty")
    merged: dict[str, dict[str, Any]] = {}
    for _event, body in parse_pretty(out).items():
        merged.update(body)
    return merged


def child(sa: dict[str, Any], name: str) -> dict[str, Any] | None:
    """The INSTALLED CHILD_SA *name* of an IKE_SA, newest first."""
    found: list[dict[str, Any]] = [
        c for c in sa.get("child-sas", {}).values() if c.get("name") == name and c.get("state") == "INSTALLED"
    ]
    found.sort(key=lambda c: int(c.get("uniqueid", "0")), reverse=True)
    return found[0] if found else None


async def established(vm: Machine, connection: str, child_name: str | None = None) -> dict[str, Any]:
    """Wait for *connection* ESTABLISHED with *child_name* INSTALLED."""

    async def check() -> dict[str, Any] | None:
        sa = (await sas(vm)).get(connection)
        if not sa or sa.get("state") != "ESTABLISHED":
            return None
        if child(sa, child_name or connection) is None:
            return None
        return sa

    return await until(vm, f"IKE_SA {connection} up", check)


async def vtysh(vm: Machine, command: str) -> dict[str, Any]:
    """Run a `vtysh` command with `json` appended, and parse its object."""
    value: object = json.loads(await vm.succeed(f"vtysh -c '{command} json'"))
    if not isinstance(value, dict):
        raise AssertionError(f"vtysh {command!r} printed {type(value).__name__}, not an object")
    return value


async def bgp_peers(vm: Machine) -> dict[str, dict[str, Any]]:
    summary = await vtysh(vm, "show bgp ipv4 unicast summary")
    # FRR puts the peers at the top level on some releases and under `ipv4Unicast` on others.
    peers: dict[str, dict[str, Any]] = summary.get("peers", summary.get("ipv4Unicast", {}).get("peers", {}))
    return peers


async def bgp_up(vm: Machine, peers: list[str], timeout: float = CONVERGE) -> dict[str, dict[str, Any]]:
    async def check() -> dict[str, dict[str, Any]] | None:
        state = await bgp_peers(vm)
        if all(state.get(p, {}).get("state") == "Established" for p in peers):
            return state
        return None

    return await until(vm, f"BGP with {', '.join(peers)}", check, timeout)


def nexthops(route: str) -> list[str]:
    """Devices a route in `ip route show` output goes out of."""
    return re.findall(r"dev (\S+)", route)


async def capture(
    vm: Machine, interface: str, expression: str, count: int, seconds: float, flags: str = ""
) -> tuple[int, str]:
    """tcpdump until *count* packets or *seconds*; rc 124 means it timed out."""
    return await vm.execute(
        f"timeout {seconds:.0f} tcpdump -n -l {flags} -i {interface} -c {count} '{expression}' 2>&1",
        timeout=seconds + 10,
    )


async def captured_during(
    vm: Machine, interface: str, expression: str, count: int, action: Callable[[], Awaitable[object]], flags: str = ""
) -> tuple[int, str]:
    """Start a capture, run *action* once it listens, return what it saw."""
    result: dict[str, tuple[int, str]] = {}

    async def watch() -> None:
        result["capture"] = await capture(vm, interface, expression, count, 10, flags)

    async with anyio.create_task_group() as tg:
        tg.start_soon(watch)
        await anyio.sleep(2)
        await action()
    return result["capture"]


async def route_mtu(vm: Machine, address: str) -> int | None:
    """The path MTU the kernel has cached for *address*, if any."""
    match = re.search(r"\bmtu (\d+)", await vm.succeed(f"ip route get {address}"))
    return int(match.group(1)) if match else None


async def journal_since(vm: Machine, unit: str, since: float) -> str:
    return await vm.succeed(f"journalctl -u {unit} -o cat --since @{int(since)} --no-pager")


async def epoch(vm: Machine) -> float:
    return float(await vm.succeed("date +%s.%N"))


async def xfrm_stats(vm: Machine) -> dict[str, int]:
    out = await vm.succeed("cat /proc/net/xfrm_stat")
    return {name: int(value) for name, value in (line.split() for line in out.splitlines() if line.strip())}


def loss(ping_output: str) -> tuple[int, int]:
    """(transmitted, received) from ping's summary line."""
    match = re.search(r"(\d+) packets transmitted, (\d+) received", ping_output)
    if not match:
        raise AssertionError(f"no ping summary in:\n{ping_output}")
    return int(match.group(1)), int(match.group(2))
