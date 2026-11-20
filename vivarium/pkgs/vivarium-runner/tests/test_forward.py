from __future__ import annotations

import functools
import re
import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import cast

import anyio
import pytest
from vivarium_runner import Machine, MachineError, forward
from vivarium_runner.forward import ForwardError, PortMap, Rule, UplinkNetwork
from vivarium_runner.machine_spec import MachineSpec

FIRST = forward.LOOPBACK
"""The address `resolve` gives every guest."""


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


def test_an_inbound_entry_comes_from_json() -> None:
    entry = Rule.from_json({"guest": 6443, "host": None, "protocols": ["tcp", "udp"], "address": None})
    assert entry == Rule(address=None, ports=(PortMap(host=None, guest=6443),), protocols=("tcp", "udp"))


def test_an_unset_host_port_is_any_free_one_that_the_claim_holds() -> None:
    [rule], [held] = forward.claim(forward.resolve([Rule(ports=(PortMap(None, 80),))]), random=False)
    try:
        [claimed] = rule.ports
        assert claimed.guest == 80
        assert claimed.host is not None
        assert claimed.host >= 1024
        assert held.sock.getsockname() == (FIRST, claimed.host)
        assert forward.reachable([rule], 80) == [f"{FIRST}:{claimed.host}"]
    finally:
        held.sock.close()


def test_reachable_tells_the_protocols_apart() -> None:
    rules = [
        Rule(address="127.0.0.3", ports=(PortMap(14000, 53),)),
        Rule(address="127.0.0.3", ports=(PortMap(15000, 53),), protocols=("udp",)),
    ]
    assert forward.reachable(rules, 53) == ["127.0.0.3:14000"]
    assert forward.reachable(rules, 53, "udp") == ["127.0.0.3:15000"]


def test_a_privileged_fixed_host_port_fails_by_name() -> None:
    with pytest.raises(ForwardError, match=r"^host port 80 for guest port 8080 needs privileges"):
        forward.resolve([Rule(address="127.0.0.3", ports=(PortMap(80, 8080),))], start=1024)


def test_random_claims_ignore_a_fixed_port() -> None:
    port = free_port_above(FIRST, 20000)
    [rule] = forward.resolve([Rule(ports=(PortMap(port, 22),))], start=1024)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind((FIRST, port))
        holder.listen()
        [claimed], held = forward.claim([rule], random=True)
    for one in held:
        one.sock.close()
    assert claimed.ports[0].host != port


def test_every_guest_gets_the_one_loopback_address() -> None:
    port = free_port_above(FIRST, 25000)
    [first] = forward.resolve([Rule(ports=(PortMap(port, port),))], start=1024)
    [second] = forward.resolve([Rule(ports=(PortMap(port, port),))], start=1024)
    assert first == second == Rule(address=FIRST, ports=(PortMap(port, port),))
    [named] = forward.resolve([Rule(address="0.0.0.0", ports=(PortMap(port, port),))], start=1024)
    assert named.address == "0.0.0.0"


@dataclass(frozen=True)
class _Spec:
    """The one field of a `MachineSpec` that `resolve_forward` reads."""

    forwards_through_egress: bool


class Forwarding(Machine):
    """A machine with forwards and nothing else."""

    def __init__(self, rules: list[Rule], *, egress: bool = True) -> None:
        self.forward = rules
        self.on_console = None
        self.spec = cast(MachineSpec, _Spec(forwards_through_egress=egress))
        self.random_ports = False
        self.claims = []

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
            vm.resolve_forward()


def test_a_forward_with_no_egress_fails_the_guest_by_name() -> None:
    vm = Forwarding([Rule(address=FIRST, ports=(PortMap(None, 80),))], egress=False)
    with pytest.raises(MachineError, match=r"^\[web\] forward: no egress serves this guest's uplink"):
        vm.resolve_forward()


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


def test_two_guests_on_one_loopback_never_share_a_port() -> None:
    """A fixed port a second guest asks for fails; the evaluation refuses
    that run first. Two unset ports get two numbers while both are held."""
    port = free_port_above(FIRST, 24000)
    [first], held = forward.claim(forward.resolve([Rule(ports=(PortMap(port, port),))]), random=False)
    try:
        assert (first.address, first.ports) == (FIRST, (PortMap(port, port),))
        with pytest.raises(ForwardError, match=rf"{re.escape(FIRST)}:{port} is already bound"):
            forward.claim(forward.resolve([Rule(ports=(PortMap(port, port),))]), random=False)
        [one], more = forward.claim(forward.resolve([Rule(ports=(PortMap(None, 1),))]), random=False)
        held += more
        [two], more = forward.claim(forward.resolve([Rule(ports=(PortMap(None, 1),))]), random=False)
        held += more
        assert len({port, one.ports[0].host, two.ports[0].host}) == 3
    finally:
        for one_held in held:
            one_held.sock.close()


@dataclass(frozen=True)
class _EgressSpec:
    """What a runtime add or remove reads of a `MachineSpec`."""

    forwards_through_egress: bool
    uplink_address: str | None = "10.0.2.16"


class Runtime(Machine):
    """A guest whose forwards an egress serves, and nothing else."""

    def __init__(self, control: socket.socket | None, *, egress: bool = True) -> None:
        self.spec = cast(MachineSpec, _EgressSpec(forwards_through_egress=egress))
        self.on_console = None
        self.on_inbound = None
        self.random_ports = False
        self.claims = []
        self.forward = []
        self.declared = []
        self.behind = None
        self.egress_relay = "/r"
        self.egress_control = control
        self._egress_lock = anyio.Lock()

    @property
    def name(self) -> str:
        return "web"


@pytest.fixture
def egress() -> Iterator[tuple[socket.socket, list[tuple[bytes, int]]]]:
    """The runner's end of a control socket, and what a stand-in egress
    heard on it: each line, and how many sockets came with it."""
    ours, theirs = socket.socketpair()
    heard: list[tuple[bytes, int]] = []

    def answer() -> None:
        while True:
            line, fds, _, _ = socket.recv_fds(theirs, 512, 1)
            if not line:
                return
            for fd in fds:
                socket.close(fd)
            heard.append((line, len(fds)))
            theirs.sendall(b"ok\n")

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    yield ours, heard
    ours.close()
    thread.join(5)
    theirs.close()


def test_a_runtime_add_and_remove_reach_the_egress_and_the_port(
    egress: tuple[socket.socket, list[tuple[bytes, int]]],
) -> None:
    control, heard = egress
    vm = Runtime(control)
    told: list[str] = []
    vm.on_inbound = lambda one: told.append(one.name)

    async def main() -> None:
        [where] = await vm.open_inbound(8080, protocols=("tcp", "udp"))
        address, _, port = where.rpartition(":")
        assert address == FIRST
        assert vm.reachable(8080) == vm.reachable(8080, "udp") == [where]
        assert [(one.protocol, one.guest) for one in vm.claims] == [("tcp", 8080), ("udp", 8080)]
        with pytest.raises(MachineError, match="forwarded already"):
            await vm.open_inbound(8080)
        await vm.close_inbound(8080, "udp")
        assert vm.reachable(8080, "udp") == []
        assert vm.reachable(8080) == [where]
        await vm.close_inbound(8080)
        assert vm.reachable(8080) == []
        assert (vm.claims, vm.forward, vm.declared) == ([], [], [])
        # The runner let go of its copy: the port binds again.
        with socket.socket() as sock:
            sock.bind((address, int(port)))

    anyio.run(main)
    assert [line.split()[0] for line, _ in heard] == [b"add", b"add", b"remove", b"remove"]
    assert [count for _, count in heard] == [1, 1, 0, 0]
    assert heard[0][0] == b"add tcp 10.0.2.16:8080 /r\n"
    assert heard[2][0].startswith(b"remove udp ")
    assert told == ["web", "web", "web"]


def test_a_runtime_add_while_the_egress_is_down_waits_for_the_next_boot() -> None:
    """The claim is kept; the next boot's hand-over carries it."""
    vm = Runtime(None)

    async def main() -> None:
        await vm.open_inbound(9000)
        assert [one.guest for one in vm.claims] == [9000]
        await vm.close_inbound(9000)

    anyio.run(main)
    assert vm.claims == []


def test_the_diff_names_what_a_switch_drops_and_adds() -> None:
    ssh, web = Rule(ports=(PortMap(None, 22),)), Rule(ports=(PortMap(None, 80),))
    moved = Rule(ports=(PortMap(18080, 80),))
    assert forward.diff([ssh, web], [ssh, web]) == ([], [])
    assert forward.diff([ssh, web], [ssh, moved]) == ([web], [moved])
    assert forward.diff([ssh], []) == ([ssh], [])


def test_a_switch_keeps_a_staying_port_and_rolls_back_on_failure(
    egress: tuple[socket.socket, list[tuple[bytes, int]]],
) -> None:
    control, heard = egress
    vm = Runtime(control)
    web, dns = Rule(ports=(PortMap(None, 80),)), Rule(ports=(PortMap(None, 53),))

    async def switched() -> str:
        return "switched"

    async def broken() -> str:
        raise MachineError("switch-to-configuration failed")

    async def main() -> None:
        [before] = await vm.open_inbound(80)
        with pytest.raises(MachineError, match="switch-to-configuration failed"):
            await vm.switch_inbound([web, dns], broken)
        assert vm.declared == [web]
        assert vm.reachable(53) == []
        assert await vm.switch_inbound([web, dns], switched) == "switched"
        assert vm.reachable(80) == [before]
        assert vm.reachable(53) != []
        assert await vm.switch_inbound([dns], switched) == "switched"
        assert (vm.declared, vm.reachable(80)) == ([dns], [])

    anyio.run(main)
    assert [line.split()[:2] for line, _ in heard] == [
        [b"add", b"tcp"],  # 80
        [b"add", b"tcp"],  # 53, then the broken switch
        [b"remove", b"tcp"],  # 53 again
        [b"add", b"tcp"],  # 53
        [b"remove", b"tcp"],  # 80
    ]


def test_a_guest_with_no_egress_refuses_a_runtime_change() -> None:
    vm = Runtime(None, egress=False)
    with pytest.raises(MachineError, match="no egress serves this guest's uplink"):
        anyio.run(functools.partial(vm.open_inbound, 9000))


NETWORK = UplinkNetwork(
    address="192.0.2.15", prefix_length=24, gateway="192.0.2.2", dns="192.0.2.3", gateway6="fd00:9::2"
)


def test_the_uplink_args_say_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    """The DNS and offline lines are `egress_notes`, which the guest says under its name."""
    forward.egress_uplink_args(NETWORK, offline=True)
    assert capsys.readouterr().out == ""


def test_the_args_carry_the_spec_s_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(forward.ENV_DNS, "192.0.2.53")
    assert forward.egress_uplink_args(NETWORK) == ["--map=192.0.2.3:53=192.0.2.53:53", "--loopback=fd00:9::2"]
    assert NETWORK.subnet == "192.0.2.0/24"


def test_the_notes_name_the_upstream_and_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(forward.ENV_DNS, "192.0.2.53")
    [router, offline] = forward.egress_notes(NETWORK, offline=True)
    assert router == "[uplink] router: DNS on 192.0.2.3, forwarded to 192.0.2.53"
    assert offline.startswith(f"[uplink] router offline: outbound bound to {forward.OFFLINE_OUTBOUND}")


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
