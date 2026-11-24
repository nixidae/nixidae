"""Watch Services and Nodes; give each LoadBalancer Service an address and
route it.

Level-triggered: any watch event marks the cluster changed, and one
reconcile reads every Service and Node and makes the routes and the status
match. A missed or doubled event changes nothing.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import anyio.abc
import httpx
import kr8s
import kr8s.asyncio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from .model import ServiceKey, allocate, parse_node, parse_service, plan_routes, status_changes, status_patch
from .routes import Table

_log = logging.getLogger(__name__)

RESYNC = 300.0
"""Seconds between reconciles with no event: a route someone removed by
hand comes back. Nothing tells the controller about that."""

RETRY = 5.0
FIELD_MANAGER = "vivarium-lb"


@dataclass(frozen=True, slots=True)
class Config:
    serviceaccount: Path
    pool: ipaddress.IPv4Network
    table: int
    # Each node's address on the router's network, by node name.
    nodes: Mapping[str, str]
    load_balancer_class: str
    ip: str

    @classmethod
    # The config file Nix writes: JSON of a fixed shape, checked here.
    def from_json(cls, raw: Mapping[str, Any]) -> Config:
        return cls(
            serviceaccount=Path(raw["serviceaccount"]),
            pool=ipaddress.IPv4Network(raw["pool"]),
            table=int(raw["table"]),
            nodes=dict(raw["nodes"]),
            load_balancer_class=raw["loadBalancerClass"],
            ip=raw["ip"],
        )


class Controller:
    def __init__(self, config: Config, api: kr8s.asyncio.Api) -> None:
        self.config = config
        self.api = api
        self.table = Table(config.ip, config.table)

    async def _list(self, kind: str) -> list[dict[str, Any]]:
        namespace = kr8s.ALL if kind == "services" else None
        found: list[dict[str, Any]] = []
        async for item in self.api.get(kind, namespace=namespace, raw=True):
            if isinstance(item, dict):
                found.append(item)
        return found

    async def _patch_status(self, key: ServiceKey, address: ipaddress.IPv4Address | None) -> None:
        namespace, name = key
        async with self.api.call_api(
            "PATCH",
            namespace=namespace,
            url=f"services/{name}/status",
            content=json.dumps(status_patch(address)),
            headers={"Content-Type": "application/merge-patch+json"},
        ):
            pass

    async def reconcile(self) -> None:
        klass = self.config.load_balancer_class
        services = [parse_service(raw, klass) for raw in await self._list("services")]
        nodes = [parse_node(raw) for raw in await self._list("nodes")]
        assigned = allocate(services, self.config.pool)
        unplaced = sorted(service.key for service in services if service.ours and service.key not in assigned)
        if unplaced:
            _log.warning(
                "no address for %s: the pool %s is full or the asked address taken", unplaced, self.config.pool
            )
        # Routes first: a client that reads the status finds the address routed.
        added, removed = await self.table.converge(plan_routes(assigned, nodes, self.config.nodes))
        for route in added:
            _log.info("route %s via %s", route.destination, ", ".join(route.gateways))
        for route in removed:
            _log.info("route %s removed", route.destination)
        for key, address in status_changes(services, assigned, self.config.pool).items():
            _log.info("%s/%s: %s", *key, address or "no address")
            await self._patch_status(key, address)

    async def watch(self, kind: str, changed: MemoryObjectSendStream[None]) -> None:
        namespace = kr8s.ALL if kind == "services" else None
        while True:
            try:
                async for _event, _obj in self.api.watch(kind, namespace=namespace):
                    nudge(changed)
            except (kr8s.ServerError, httpx.HTTPError, OSError) as error:
                _log.warning("watch %s: %s; again in %.0f s", kind, error, RETRY)
            await anyio.sleep(RETRY)
            nudge(changed)

    async def reconciler(self, changed: MemoryObjectReceiveStream[None]) -> None:
        while True:
            try:
                await self.reconcile()
            except (kr8s.ServerError, httpx.HTTPError, OSError, RuntimeError) as error:
                _log.warning("reconcile: %s; again in %.0f s", error, RETRY)
                with anyio.move_on_after(RETRY):
                    await changed.receive()
                continue
            with anyio.move_on_after(RESYNC):
                await changed.receive()


def nudge(changed: MemoryObjectSendStream[None]) -> None:
    """Mark the cluster changed. One pending mark is enough: the reconcile
    reads everything."""
    with contextlib.suppress(anyio.WouldBlock):
        changed.send_nowait(None)


async def run(config: Config) -> None:
    # No `url`: kr8s reads the ServiceAccount's CA only without one, and
    # takes the server from KUBERNETES_SERVICE_HOST and _PORT.
    api = await kr8s.asyncio.api(serviceaccount=str(config.serviceaccount), field_manager=FIELD_MANAGER)
    controller = Controller(config, api)
    changed, receive = anyio.create_memory_object_stream[None](1)
    async with anyio.create_task_group() as group:
        group.start_soon(controller.reconciler, receive)
        group.start_soon(controller.watch, "services", changed)
        group.start_soon(controller.watch, "nodes", changed)
