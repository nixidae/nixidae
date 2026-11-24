"""The router's routes this controller owns, through `ip`.

Its routes carry their own protocol number, so a restart finds and
replaces what an earlier run left, and never touches another's.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Iterable, Sequence
from typing import Any, Final

import anyio

from .model import Route

PROTOCOL: Final = 244
"""rtnetlink protocol number of the routes this controller adds. Not one
`/etc/iproute2/rt_protos` names."""


# `ip -json route` output: arbitrary JSON from iproute2.
def parse(raw: Sequence[Any]) -> set[Route]:
    """`ip -json route show` output as routes. A route's `dst` is a prefix,
    or an address for a /32."""
    routes = set()
    for entry in raw:
        destination = ipaddress.ip_network(entry["dst"])
        if not isinstance(destination, ipaddress.IPv4Network):
            continue
        hops = entry.get("nexthops") or [entry]
        gateways = tuple(sorted(hop["gateway"] for hop in hops if "gateway" in hop))
        routes.add(Route(destination, gateways))
    return routes


def replace_args(route: Route, table: int) -> list[str]:
    args = ["route", "replace", str(route.destination), "table", str(table), "proto", str(PROTOCOL)]
    if len(route.gateways) == 1:
        return [*args, "via", route.gateways[0]]
    return args + [word for gateway in route.gateways for word in ("nexthop", "via", gateway)]


def delete_args(route: Route, table: int) -> list[str]:
    return ["route", "delete", str(route.destination), "table", str(table), "proto", str(PROTOCOL)]


class Table:
    def __init__(self, ip: str, table: int) -> None:
        self.ip = ip
        self.table = table

    async def _ip(self, *args: str) -> str:
        done = await anyio.run_process([self.ip, *args], check=False)
        if done.returncode != 0:
            raise RuntimeError(f"ip {' '.join(args)}: {done.stderr.decode().strip()}")
        return done.stdout.decode()

    async def current(self) -> set[Route]:
        out = await self._ip("-json", "route", "show", "table", str(self.table), "proto", str(PROTOCOL))
        return parse(json.loads(out or "[]"))

    async def converge(self, desired: Iterable[Route]) -> tuple[set[Route], set[Route]]:
        """Make the table hold *desired*; returns what it added and removed."""
        want = {Route(route.destination, tuple(sorted(route.gateways))) for route in desired}
        have = await self.current()
        added, removed = want - have, have - want
        stale = {route.destination for route in removed} - {route.destination for route in added}
        for route in added:
            await self._ip(*replace_args(route, self.table))
        for route in removed:
            if route.destination in stale:
                await self._ip(*delete_args(route, self.table))
        return added, {route for route in removed if route.destination in stale}
