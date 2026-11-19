from __future__ import annotations

import re
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import pytest
from vivarium_runner import Machine, MachineError, forward
from vivarium_runner.forward import ForwardError, PortMap, Rule, UplinkNetwork
from vivarium_runner.machine_spec import MachineSpec

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


def test_passt_gets_one_spec_per_protocol_in_host_port_order() -> None:
    """passt reads `ADDR/HOST:GUEST,...`, and a port mapped to itself as one number."""
    rule = Rule(address="127.0.0.3", ports=(PortMap(14325, 22), PortMap(8080, 8080)), protocols=("tcp", "udp"))
    assert forward.to_args([rule]) == [
        "--tcp-ports",
        "127.0.0.3/8080,14325:22",
        "--udp-ports",
        "127.0.0.3/8080,14325:22",
    ]


@linux_loopback
def test_each_guest_gets_a_loopback_address_no_other_guest_has() -> None:
    port = free_port_above("127.0.0.3", 25000)
    taken = {"127.0.0.2"}
    [first], _ = forward.resolve([Rule(ports=(PortMap(port, port),))], taken=taken, start=1024)
    [second], _ = forward.resolve([Rule(ports=(PortMap(port, port),))], taken=taken, start=1024)
    assert first.address == "127.0.0.3"
    assert second.address not in {None, "127.0.0.2", "127.0.0.3"}
    assert taken == {"127.0.0.2", "127.0.0.3", second.address}
    assert first.ports == second.ports == (PortMap(port, port),)


@dataclass(frozen=True)
class _Spec:
    """The one field of a `MachineSpec` that `resolve_forward` reads."""

    forwards_through_egress: bool


class Forwarding(Machine):
    """A machine with forwards and nothing else."""

    def __init__(self, rules: list[Rule], *, egress: bool = False) -> None:
        self.forward = rules
        self.on_console = None
        self.spec = cast(MachineSpec, _Spec(forwards_through_egress=egress))
        self.random_ports = False
        self.claims = []

    @property
    def name(self) -> str:
        return "web"


@pytest.mark.parametrize("egress", [False, True], ids=["probed", "claimed"])
def test_a_forward_that_cannot_be_set_up_fails_the_guest_by_name(egress: bool) -> None:
    """`Session.prepare` reports a `MachineError` as a failed boot; a bare
    `ForwardError` ended the run as a traceback (nixidae/nixidae#1335). A
    forward through an egress fails at its claim, a passt one at its probe."""
    port = free_port_above(FIRST, 22000)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind((FIRST, port))
        holder.listen()
        vm = Forwarding([Rule(address=FIRST, ports=(PortMap(port, port),))], egress=egress)
        escaped = FIRST.replace(".", r"\.")
        with pytest.raises(
            MachineError, match=rf"^\[web\] forward: cannot forward .*{escaped}:{port} is already bound"
        ):
            vm.resolve_forward(set())


def test_a_claim_holds_its_port_until_it_is_closed() -> None:
    """The bind is the claim: while it lives, a second bind of the port
    fails, on macOS too, where SO_REUSEADDR is looser (nixidae/nixidae#1546)."""
    port = free_port_above(FIRST, 23000)
    rules = [Rule(address=FIRST, ports=(PortMap(port, 22),))]
    [rule], [held] = forward.claim(rules, random=False)
    assert rule.ports == (PortMap(port, 22),)
    assert (held.protocol, held.guest, held.sock.getsockname()) == ("tcp", 22, (FIRST, port))
    with pytest.raises(ForwardError, match=rf"{re.escape(FIRST)}:{port} is already bound"):
        forward.claim(rules, random=False)
    held.sock.close()
    _, [again] = forward.claim(rules, random=False)
    again.sock.close()


def test_random_claims_of_one_port_get_one_number_each() -> None:
    """Two runs that both want 4325 both start; TCP and UDP of one forward share a number."""
    rules = [Rule(address=FIRST, ports=(PortMap(4325, 22),), protocols=("tcp", "udp"))]
    [first], held = forward.claim(rules, random=True)
    [second], more = forward.claim(rules, random=True)
    try:
        ours, theirs = first.ports[0].host, second.ports[0].host
        assert ours != theirs
        assert [(one.protocol, one.sock.getsockname()[1], one.guest) for one in held] == [
            ("tcp", ours, 22),
            ("udp", ours, 22),
        ]
    finally:
        for one in (*held, *more):
            one.sock.close()


def test_a_failed_claim_lets_go_of_what_it_bound() -> None:
    port = free_port_above(FIRST, 24000)
    taken = free_port_above(FIRST, port + 1)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind((FIRST, taken))
        holder.listen()
        with pytest.raises(ForwardError, match=rf":{taken} is already bound"):
            forward.claim([Rule(address=FIRST, ports=(PortMap(port, 1), PortMap(taken, 2)))], random=False)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((FIRST, port))


def test_the_egress_line_names_the_guest_port() -> None:
    """The line `parse_add` reads (`a_control_add_parses` in vivarium-egress)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        add = forward.egress_adds([forward.Claim("udp", sock, 53)], "10.0.2.16", "vsock:5001")
        assert [one.line() for one in add] == [b"add udp 10.0.2.16:53 vsock:5001\n"]


def test_a_privileged_port_reaches_the_guest_at_its_offset() -> None:
    """Where `reachable` points is the port the runner claims."""
    [rule], notes = forward.resolve([Rule(address="127.0.0.3", ports=(PortMap(80, 80),))], taken=set(), start=1024)
    assert forward.reachable([rule], 80) == ["127.0.0.3:10080"]
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


NETWORK = UplinkNetwork(
    address="192.0.2.15", prefix_length=24, gateway="192.0.2.2", dns="192.0.2.3", gateway6="fd00:9::2"
)


def test_the_uplink_args_say_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    """The DNS and offline lines are `uplink_notes`, which the guest says under its name."""
    forward.uplink_args(NETWORK, offline=True)
    forward.egress_uplink_args(NETWORK, offline=True)
    assert capsys.readouterr().out == ""


def test_the_args_carry_the_spec_s_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(forward.ENV_DNS, "192.0.2.53")
    passt = forward.uplink_args(NETWORK)
    assert passt[:8] == [
        "--address",
        "192.0.2.15",
        "--netmask",
        "24",
        "--gateway",
        "192.0.2.2",
        "--dns-forward",
        "192.0.2.3",
    ]
    assert forward.egress_uplink_args(NETWORK) == ["--map=192.0.2.3:53=192.0.2.53:53", "--loopback=fd00:9::2"]
    assert NETWORK.subnet == "192.0.2.0/24"


def test_the_notes_name_the_upstream_and_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(forward.ENV_DNS, "192.0.2.53")
    assert forward.uplink_notes(NETWORK) == ["[uplink] DNS on 192.0.2.3, forwarded to 192.0.2.53"]
    [_, offline] = forward.uplink_notes(NETWORK, offline=True)
    assert offline.startswith(f"[uplink] offline: outbound bound to {forward.OFFLINE_OUTBOUND}")
    [router, _] = forward.egress_notes(NETWORK, offline=True)
    assert router == "[uplink] router: DNS on 192.0.2.3, forwarded to 192.0.2.53"


class Speaking(Machine):
    """A machine that says things and has nothing else."""

    def __init__(self) -> None:
        self.on_console: Callable[[str, str], None] | None = None
        self.on_say: Callable[[str, str], None] | None = None

    @property
    def name(self) -> str:
        return "cp"


def test_say_goes_to_on_say_and_else_to_the_console() -> None:
    vm = Speaking()
    heard: list[tuple[str, str]] = []
    vm.on_console = lambda name, line: heard.append(("console", f"{name}: {line}"))
    vm.say("[k8s] joined")
    assert heard == [("console", "cp: [k8s] joined")]
    vm.on_say = lambda name, text: heard.append(("say", f"{name}: {text}"))
    vm.say("[k8s] Ready")
    assert heard[1:] == [("say", "cp: [k8s] Ready")]
