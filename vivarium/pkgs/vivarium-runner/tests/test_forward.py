from __future__ import annotations

import socket

import pytest
from vivarium_runner import forward
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
    picked = rule.ports[1]
    assert picked.guest == guest
    assert picked.host is not None
    assert picked.host > guest
    assert notes == [f"{rule.address}: guest port {guest} on host port {picked.host}: {guest} is taken"]
    assert forward.reachable([rule], guest, 1024) == [f"{rule.address}:{picked.host}"]


def test_never_an_ephemeral_port() -> None:
    low, high = forward.ephemeral_range()
    [rule], _ = forward.resolve([Rule(ports=(PortMap(None, low),))], taken=set(), start=1024)
    [picked] = rule.ports
    assert picked.host is not None
    assert not low <= picked.host <= high


def test_passt_never_gets_an_unpicked_port() -> None:
    with pytest.raises(ForwardError):
        forward.to_args([Rule(address="127.0.0.2", ports=(PortMap(None, 6443),))], start=1024)
