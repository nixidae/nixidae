from __future__ import annotations

import socket

import pytest
from vivarium_runner import Machine, MachineError, forward
from vivarium_runner.forward import ForwardError, PortMap, Rule


def free_port_above(address: str, start: int) -> int:
    """A port that binds now on *address*, at or above *start*."""
    for port in range(start, 65536):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((address, port))
            except OSError:
                continue
            return port
    raise AssertionError(f"no free port on {address} above {start}")


def test_a_port_map_with_no_host_port_comes_from_json() -> None:
    assert PortMap.from_json({"host": None, "guest": 6443}) == PortMap(host=None, guest=6443)


def test_the_guests_own_port_when_it_is_free() -> None:
    guest = free_port_above("127.0.0.2", 20000)
    [rule], notes = forward.resolve([Rule(ports=(PortMap(None, guest),))], taken=set(), start=1024)
    assert rule.ports == (PortMap(guest, guest),)
    assert notes == []


def test_the_next_free_port_when_the_guests_is_taken() -> None:
    """A host that runs its own cluster holds *:6443, and every loopback
    address with it. Measured on dynhetz."""
    guest = free_port_above("127.0.0.2", 21000)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("0.0.0.0", guest))  # noqa: S104 -- the wildcard is the case under test
        holder.listen()
        [rule], notes = forward.resolve(
            [Rule(ports=(PortMap(4325, 4325), PortMap(None, guest)))], taken=set(), start=1024
        )
    assert not isinstance(rule.ports, str)
    picked = rule.ports[1]
    assert picked.guest == guest
    assert picked.host is not None
    assert picked.host > guest
    assert notes == [f"{rule.address}: guest port {guest} on host port {picked.host}: {guest} is taken"]
    assert forward.reachable([rule], guest, 1024) == [f"{rule.address}:{picked.host}"]


def test_never_an_ephemeral_port() -> None:
    low, high = forward.ephemeral_range()
    [rule], _ = forward.resolve([Rule(ports=(PortMap(None, low),))], taken=set(), start=1024)
    assert not isinstance(rule.ports, str)
    [picked] = rule.ports
    assert picked.host is not None
    assert not low <= picked.host <= high


def test_passt_never_gets_an_unpicked_port() -> None:
    with pytest.raises(ForwardError):
        forward.to_args([Rule(address="127.0.0.2", ports=(PortMap(None, 6443),))], start=1024)


class Forwarding(Machine):
    """A machine with forwards and nothing else."""

    def __init__(self, rules: list[Rule]) -> None:
        self.forward = rules
        self.on_console = None

    @property
    def name(self) -> str:
        return "web"


def test_a_forward_that_cannot_be_set_up_fails_the_guest_by_name() -> None:
    """`Session.prepare` reports a `MachineError` as a failed boot; a bare
    `ForwardError` ended the run as a traceback (nixidae/nixidae#1335)."""
    port = free_port_above("127.0.0.2", 22000)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.2", port))
        holder.listen()
        vm = Forwarding([Rule(address="127.0.0.2", ports=(PortMap(port, port),))])
        with pytest.raises(MachineError, match=rf"^\[web\] forward: cannot forward .*127\.0\.0\.2:{port} is already bound"):
            vm.resolve_forward(set())


def _guest_of(maps: list[forward.Mapping], address: str, host: int) -> int | None:
    for m in maps:
        if m.address == address and m.first <= host <= m.last:
            return m.guest + host - m.first
    return None


@pytest.mark.parametrize("remap", [True, False])
def test_the_egress_binds_where_reachable_points(monkeypatch: pytest.MonkeyPatch, remap: bool) -> None:
    """Every `addr:port` that `reachable` names reaches that guest port."""
    monkeypatch.setattr(forward, "ephemeral_range", lambda: (32768, 60999))
    rule = Rule(address="127.0.0.2", remap_privileged=remap)
    maps = forward.mappings(rule, 1024)
    for guest in (1, 22, 443, 1023, 1024, 6443, 10001, 11023, 11024, 32767, 61000, 65535):
        for found in forward.reachable([rule], guest, 1024):
            address, port = found.rsplit(":", 1)
            assert _guest_of(maps, address, int(port)) == guest, found
    assert _guest_of(maps, "127.0.0.2", 40000) is None, "the ephemeral range stays the host's"
    spans = sorted((m.first, m.last) for m in maps)
    assert all(a[1] < b[0] for a, b in zip(spans, spans[1:], strict=False)), "no host port is bound twice"


def test_a_wide_rule_is_weak_remap_included(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(forward, "ephemeral_range", lambda: (32768, 60999))
    rule = Rule(address="127.0.0.2", protocols=("tcp", "udp"))
    args = forward.egress_args([rule], "10.0.2.15", "vsock:5001", start=1024)
    assert args == [
        line
        for proto in ("tcp", "udp")
        for line in (
            f"~{proto}/127.0.0.2:1024-10000=10.0.2.15:1024@vsock:5001",
            f"~{proto}/127.0.0.2:11024-32767=10.0.2.15:11024@vsock:5001",
            f"~{proto}/127.0.0.2:61000-65535=10.0.2.15:61000@vsock:5001",
            f"~{proto}/127.0.0.2:10001-11023=10.0.2.15:1@vsock:5001",
        )
    ]


def test_listed_ports_are_strict() -> None:
    rule = Rule(address="127.0.0.3", ports=(PortMap(14325, 22), PortMap(8080, 8080)))
    assert forward.egress_args([rule], "10.0.2.16", "vsock:5001", start=1024) == [
        "tcp/127.0.0.3:14325-14325=10.0.2.16:22@vsock:5001",
        "tcp/127.0.0.3:8080-8080=10.0.2.16:8080@vsock:5001",
    ]
