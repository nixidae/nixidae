"""What the controller reads of a cluster, and what it decides. Pure."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

type ServiceKey = tuple[str, str]
"""namespace, name"""

UPLINK_LABEL = "vivarium.dev/uplink-address"
"""The Node label that holds the node's address on the router's network. Its
kubelet sets it at registration (modules/k8s.nix, `vivarium-k8s-join`), so a
node added at run time carries it too."""


@dataclass(frozen=True, slots=True)
class Service:
    key: ServiceKey
    # `type: LoadBalancer` with no class, or with ours.
    ours: bool
    requested: ipaddress.IPv4Address | None
    # What `status.loadBalancer.ingress` holds now.
    current: tuple[ipaddress.IPv4Address, ...]


@dataclass(frozen=True, slots=True)
class Node:
    name: str
    pod_cidrs: tuple[ipaddress.IPv4Network, ...]
    # Its address on the router's network; None for a node not behind the router.
    uplink: ipaddress.IPv4Address | None = None


@dataclass(frozen=True, slots=True)
class Route:
    destination: ipaddress.IPv4Network
    gateways: tuple[str, ...]


def _ipv4(text: object) -> ipaddress.IPv4Address | None:
    if not isinstance(text, str):
        return None
    try:
        return ipaddress.IPv4Address(text)
    except ValueError:
        return None


# A Service as the API sends it: arbitrary JSON, read field by field.
def parse_service(raw: Mapping[str, Any], load_balancer_class: str) -> Service:
    meta, spec = raw["metadata"], raw.get("spec", {})
    ingress: list[Mapping[str, Any]] = raw.get("status", {}).get("loadBalancer", {}).get("ingress", []) or []
    current = tuple(ip for entry in ingress if (ip := _ipv4(entry.get("ip"))) is not None)
    klass = spec.get("loadBalancerClass")
    return Service(
        key=(meta["namespace"], meta["name"]),
        ours=spec.get("type") == "LoadBalancer" and klass in (None, load_balancer_class),
        requested=_ipv4(spec.get("loadBalancerIP")),
        current=current,
    )


# A Node as the API sends it.
def parse_node(raw: Mapping[str, Any]) -> Node:
    spec = raw.get("spec", {})
    cidrs = spec.get("podCIDRs") or ([spec["podCIDR"]] if spec.get("podCIDR") else [])
    networks = []
    for cidr in cidrs:
        network = ipaddress.ip_network(cidr)
        if isinstance(network, ipaddress.IPv4Network):
            networks.append(network)
    labels: Mapping[str, str] = raw["metadata"].get("labels") or {}
    return Node(name=raw["metadata"]["name"], pod_cidrs=tuple(networks), uplink=_ipv4(labels.get(UPLINK_LABEL)))


def allocate(services: Iterable[Service], pool: ipaddress.IPv4Network) -> dict[ServiceKey, ipaddress.IPv4Address]:
    """An address from *pool* for each Service that is ours.

    A Service keeps the address it has; one that asks for an address
    (`spec.loadBalancerIP`) gets it if it is in the pool and free; the rest
    take the lowest free ones, in order of namespace and name. A Service
    left out found the pool full.
    """
    wanting = sorted((service for service in services if service.ours), key=lambda service: service.key)
    taken: dict[ipaddress.IPv4Address, ServiceKey] = {}
    chosen: dict[ServiceKey, ipaddress.IPv4Address] = {}

    def claim(service: Service, address: ipaddress.IPv4Address | None) -> None:
        if service.key in chosen or address is None or address not in pool or address in taken:
            return
        taken[address] = service.key
        chosen[service.key] = address

    for service in wanting:
        claim(service, service.requested)
    for service in wanting:
        if service.requested is None:
            for address in service.current:
                claim(service, address)
    free = iter([address for address in pool.hosts() if address not in taken])
    for service in wanting:
        if service.key not in chosen and service.requested is None:
            fresh = next(free, None)
            if fresh is None:
                break
            claim(service, fresh)
    return chosen


def plan_routes(assigned: Mapping[ServiceKey, ipaddress.IPv4Address], nodes: Iterable[Node]) -> set[Route]:
    """The routes the router needs: each Service address to every node
    behind it (kube-proxy on any node takes it), and each node's pod
    ranges to that node."""
    behind = sorted((node for node in nodes if node.uplink is not None), key=lambda node: node.name)
    gateways = tuple(str(node.uplink) for node in behind)
    routes = set()
    if gateways:
        routes |= {Route(ipaddress.IPv4Network(f"{address}/32"), gateways) for address in assigned.values()}
    for node in behind:
        routes |= {Route(cidr, (str(node.uplink),)) for cidr in node.pod_cidrs}
    return routes


def status_patch(address: ipaddress.IPv4Address | None) -> dict[str, Any]:
    """The merge patch that makes `status.loadBalancer` say *address*."""
    ingress = [] if address is None else [{"ip": str(address), "ipMode": "VIP"}]
    return {"status": {"loadBalancer": {"ingress": ingress}}}


def status_changes(
    services: Iterable[Service],
    assigned: Mapping[ServiceKey, ipaddress.IPv4Address],
    pool: ipaddress.IPv4Network,
) -> dict[ServiceKey, ipaddress.IPv4Address | None]:
    """The Services whose status is not what *assigned* says. A Service that
    holds an address of *pool* it was not given loses it: it stopped being
    ours. Another controller's addresses are left alone."""
    changes: dict[ServiceKey, ipaddress.IPv4Address | None] = {}
    for service in services:
        want = assigned.get(service.key)
        if want is not None and service.current != (want,):
            changes[service.key] = want
        elif want is None and any(address in pool for address in service.current):
            changes[service.key] = None
    return changes
