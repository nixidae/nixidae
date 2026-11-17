from __future__ import annotations

import socket
import sys

import pytest
from vivarium_runner import Machine, MachineError, forward
from vivarium_runner.forward import ForwardError, PortMap, Rule

FIRST = "127.0.0.2" if sys.platform == "linux" else forward.SHARED_LOOPBACK
"""The address `resolve` gives a run's first guest."""

linux_loopback = pytest.mark.skipif(
    sys.platform != "linux", reason="macOS: one loopback address, and a port free on it beside a wildcard holder"
)


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
    guest = free_port_above(FIRST, 20000)
    [rule], notes = forward.resolve([Rule(ports=(PortMap(None, guest),))], taken=set(), start=1024)
    assert rule.ports == (PortMap(guest, guest),)
    assert notes == []


@linux_loopback
def test_the_next_free_port_when_the_guests_is_taken() -> None:
    """A host that runs its own cluster holds *:6443, and every loopback
    address with it. Measured on dynhetz."""
    guest = free_port_above(FIRST, 21000)
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
    assert forward.reachable([rule], guest) == [f"{rule.address}:{picked.host}"]


def test_never_an_ephemeral_port() -> None:
    low, high = forward.ephemeral_range()
    [rule], _ = forward.resolve([Rule(ports=(PortMap(None, low),))], taken=set(), start=1024)
    [picked] = rule.ports
    assert picked.host is not None
    assert not low <= picked.host <= high


def test_passt_never_gets_an_unpicked_port() -> None:
    with pytest.raises(ForwardError):
        forward.to_args([Rule(address="127.0.0.2", ports=(PortMap(None, 6443),))])


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
    port = free_port_above(FIRST, 22000)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind((FIRST, port))
        holder.listen()
        vm = Forwarding([Rule(address=FIRST, ports=(PortMap(port, port),))])
        escaped = FIRST.replace(".", r"\.")
        with pytest.raises(
            MachineError, match=rf"^\[web\] forward: cannot forward .*{escaped}:{port} is already bound"
        ):
            vm.resolve_forward(set())


def test_the_egress_gets_one_port_each() -> None:
    rule = Rule(address="127.0.0.3", ports=(PortMap(14325, 22), PortMap(8080, 8080)), protocols=("tcp", "udp"))
    assert forward.egress_args([rule], "10.0.2.16", "vsock:5001") == [
        "tcp/127.0.0.3:14325=10.0.2.16:22@vsock:5001",
        "udp/127.0.0.3:14325=10.0.2.16:22@vsock:5001",
        "tcp/127.0.0.3:8080=10.0.2.16:8080@vsock:5001",
        "udp/127.0.0.3:8080=10.0.2.16:8080@vsock:5001",
    ]


def test_a_privileged_port_reaches_the_guest_at_its_offset() -> None:
    """Where `reachable` points is what the egress binds."""
    [rule], notes = forward.resolve([Rule(address="127.0.0.3", ports=(PortMap(80, 80),))], taken=set(), start=1024)
    assert forward.reachable([rule], 80) == ["127.0.0.3:10080"]
    assert forward.egress_args([rule], "10.0.2.15", "vsock:5001") == ["tcp/127.0.0.3:10080=10.0.2.15:80@vsock:5001"]
    assert notes == ["127.0.0.3: host port 80 needs privileges we do not have; forwarding 10080 -> guest 80 instead"]


def test_an_unprivileged_host_keeps_no_privileged_port_without_remap() -> None:
    rule = Rule(address="127.0.0.3", ports=(PortMap(80, 80),), remap_privileged=False)
    assert forward.resolve([rule], taken=set(), start=1024)[0] == []


@pytest.fixture
def one_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """`resolve` as on macOS, on any host."""
    # The sys module forward reads; it exports no name `sys`.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(forward, "ephemeral_range", lambda: (49152, 65535))


@pytest.mark.usefixtures("one_loopback")
def test_two_guests_on_one_loopback_get_two_ports() -> None:
    """The second guest asking for the first one's port gets the next free one."""
    port = free_port_above(forward.SHARED_LOOPBACK, 24000)
    taken: set[str] = set()
    [first], _ = forward.resolve([Rule(ports=(PortMap(port, port),))], taken=taken, start=1024)
    [second], notes = forward.resolve([Rule(ports=(PortMap(port, port),))], taken=taken, start=1024)
    assert first.address == second.address == forward.SHARED_LOOPBACK
    assert first.ports == (PortMap(port, port),)
    [moved] = second.ports
    assert moved.guest == port
    assert moved.host is not None
    assert moved.host > port
    assert notes == [f"{forward.SHARED_LOOPBACK}: guest port {port} on host port {moved.host}: {port} is taken"]
