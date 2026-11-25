from __future__ import annotations

import ipaddress
from typing import Any

from vivarium_lb.model import (
    UPLINK_LABEL,
    Node,
    Route,
    Service,
    allocate,
    parse_node,
    parse_service,
    plan_routes,
    status_changes,
    status_patch,
)
from vivarium_lb.routes import PROTOCOL, parse, replace_args

POOL = ipaddress.IPv4Network("10.200.0.0/29")
CLASS = "vivarium.dev/router"


def ip(text: str) -> ipaddress.IPv4Address:
    return ipaddress.IPv4Address(text)


def service(name: str, *, ours: bool = True, requested: str | None = None, current: tuple[str, ...] = ()) -> Service:
    return Service(
        key=("default", name),
        ours=ours,
        requested=None if requested is None else ip(requested),
        current=tuple(ip(each) for each in current),
    )


def raw_service(**spec: Any) -> dict[str, Any]:
    return {"metadata": {"namespace": "default", "name": "web"}, "spec": spec, "status": {}}


def test_parse_service_takes_ours_only() -> None:
    assert parse_service(raw_service(type="LoadBalancer"), CLASS).ours
    assert parse_service(raw_service(type="LoadBalancer", loadBalancerClass=CLASS), CLASS).ours
    assert not parse_service(raw_service(type="LoadBalancer", loadBalancerClass="other"), CLASS).ours
    assert not parse_service(raw_service(type="ClusterIP"), CLASS).ours


def test_parse_service_reads_status_and_request() -> None:
    raw = raw_service(type="LoadBalancer", loadBalancerIP="10.200.0.5")
    raw["status"] = {"loadBalancer": {"ingress": [{"ip": "10.200.0.1"}, {"hostname": "x"}]}}
    parsed = parse_service(raw, CLASS)
    assert parsed.requested == ip("10.200.0.5")
    assert parsed.current == (ip("10.200.0.1"),)


def test_parse_node_pod_ranges() -> None:
    node = parse_node({"metadata": {"name": "cp"}, "spec": {"podCIDR": "10.244.0.0/24"}})
    assert node.pod_cidrs == (ipaddress.IPv4Network("10.244.0.0/24"),)
    assert parse_node({"metadata": {"name": "cp"}, "spec": {}}).pod_cidrs == ()


def test_parse_node_reads_its_uplink_label() -> None:
    labelled = {"metadata": {"name": "w1", "labels": {UPLINK_LABEL: "10.0.2.16"}}, "spec": {}}
    assert parse_node(labelled).uplink == ip("10.0.2.16")
    assert parse_node({"metadata": {"name": "w1", "labels": {UPLINK_LABEL: "nonsense"}}}).uplink is None


def test_allocate_keeps_requests_and_fills_lowest_free() -> None:
    chosen = allocate(
        [
            service("a"),
            service("b", current=("10.200.0.1",)),
            service("c", requested="10.200.0.3"),
            service("d", ours=False),
        ],
        POOL,
    )
    assert chosen == {
        ("default", "a"): ip("10.200.0.2"),
        ("default", "b"): ip("10.200.0.1"),
        ("default", "c"): ip("10.200.0.3"),
    }


def test_allocate_refuses_addresses_outside_the_pool_and_a_full_pool() -> None:
    small = ipaddress.IPv4Network("10.200.0.0/30")
    chosen = allocate([service("a"), service("b"), service("c"), service("x", requested="10.9.9.9")], small)
    assert set(chosen) == {("default", "a"), ("default", "b")}


def test_two_asking_for_one_address() -> None:
    chosen = allocate([service("a", requested="10.200.0.2"), service("b", requested="10.200.0.2")], POOL)
    assert chosen == {("default", "a"): ip("10.200.0.2")}


def test_routes_go_to_every_node_behind_the_router_and_pods_to_their_node() -> None:
    nodes = [
        Node("w1", (ipaddress.IPv4Network("10.244.1.0/24"),), ip("10.0.2.16")),
        Node("cp", (ipaddress.IPv4Network("10.244.0.0/24"),), ip("10.0.2.15")),
        Node("stranger", (ipaddress.IPv4Network("10.244.9.0/24"),)),
    ]
    routes = plan_routes({("default", "a"): ip("10.200.0.1")}, nodes)
    assert routes == {
        Route(ipaddress.IPv4Network("10.200.0.1/32"), ("10.0.2.15", "10.0.2.16")),
        Route(ipaddress.IPv4Network("10.244.0.0/24"), ("10.0.2.15",)),
        Route(ipaddress.IPv4Network("10.244.1.0/24"), ("10.0.2.16",)),
    }


def test_status_changes_leave_other_controllers_alone() -> None:
    services = [
        service("new"),
        service("same", current=("10.200.0.2",)),
        service("gone", ours=False, current=("10.200.0.3",)),
        service("theirs", ours=False, current=("192.0.2.1",)),
    ]
    assigned = {("default", "new"): ip("10.200.0.1"), ("default", "same"): ip("10.200.0.2")}
    assert status_changes(services, assigned, POOL) == {
        ("default", "new"): ip("10.200.0.1"),
        ("default", "gone"): None,
    }
    assert status_patch(None) == {"status": {"loadBalancer": {"ingress": []}}}


def test_ip_json_routes_and_arguments() -> None:
    raw = [
        {"dst": "10.200.0.1", "nexthops": [{"gateway": "10.0.2.16"}, {"gateway": "10.0.2.15"}]},
        {"dst": "10.244.0.0/24", "gateway": "10.0.2.15"},
    ]
    assert parse(raw) == {
        Route(ipaddress.IPv4Network("10.200.0.1/32"), ("10.0.2.15", "10.0.2.16")),
        Route(ipaddress.IPv4Network("10.244.0.0/24"), ("10.0.2.15",)),
    }
    multi = replace_args(Route(ipaddress.IPv4Network("10.200.0.1/32"), ("10.0.2.15", "10.0.2.16")), 1000)
    assert multi[-6:] == ["nexthop", "via", "10.0.2.15", "nexthop", "via", "10.0.2.16"]
    assert str(PROTOCOL) in multi
