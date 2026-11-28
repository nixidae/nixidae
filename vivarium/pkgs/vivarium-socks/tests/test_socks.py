from __future__ import annotations

import ipaddress
import socket
from collections.abc import AsyncIterator, Sequence

import anyio
import anyio.abc
import dns.resolver
import pytest
from vivarium_socks import protocol
from vivarium_socks.protocol import AddressType, ProtocolError, Reply
from vivarium_socks.resolve import IPAddress, Resolver, Service, Unresolved, Zone, pick, servers_for
from vivarium_socks.server import Resolving, serve
from vivarium_socks.split import Lab, Split

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_method_reply_picks_no_authentication() -> None:
    assert protocol.method_reply(bytes([0x02, 0x00])) == bytes([5, 0x00])
    assert protocol.method_reply(bytes([0x02])) == bytes([5, 0xFF])


def test_request_head_refuses_bind_and_unknown_types() -> None:
    with pytest.raises(ProtocolError) as bind:
        protocol.parse_request_head(bytes([5, 0x02, 0, 1]))
    assert bind.value.reply is Reply.COMMAND_NOT_SUPPORTED
    with pytest.raises(ProtocolError) as kind:
        protocol.parse_request_head(bytes([5, 0x01, 0, 9]))
    assert kind.value.reply is Reply.ADDRESS_TYPE_NOT_SUPPORTED
    with pytest.raises(ProtocolError):
        protocol.parse_request_head(bytes([4, 0x01, 0, 1]))


def test_destination_forms() -> None:
    port = (443).to_bytes(2, "big")
    assert protocol.parse_destination(AddressType.IPV4, bytes([10, 0, 0, 1]), port).host == ipaddress.IPv4Address(
        "10.0.0.1"
    )
    named = protocol.parse_destination(AddressType.DOMAIN, b"\x0aweb.local.", port)
    assert (named.host, named.port) == ("web.local", 443)
    assert protocol.address_length(AddressType.DOMAIN, 10) == 11


def test_reply_bytes() -> None:
    assert protocol.reply(Reply.HOST_UNREACHABLE) == bytes([5, 4, 0, 1, 0, 0, 0, 0, 0, 0])
    ok = protocol.reply(Reply.SUCCEEDED, ipaddress.IPv6Address("::1"), 80)
    assert ok[:4] == bytes([5, 0, 0, 4]) and ok[-2:] == (80).to_bytes(2, "big")


def test_servers_for_takes_the_longest_domain() -> None:
    domains = {"cluster.local": ["10.96.0.10"], "svc.cluster.local": ["10.96.0.11"], "lab": ["10.0.2.3"]}
    assert servers_for("web.default.svc.cluster.local.", domains) == ["10.96.0.11"]
    assert servers_for("Node.Cluster.Local", domains) == ["10.96.0.10"]
    assert servers_for("lab", domains) == ["10.0.2.3"]
    assert servers_for("example.com", domains) is None


class FakeResolver:
    def __init__(self, names: dict[str, list[IPAddress]]) -> None:
        self.names = names
        self.services: dict[tuple[str, int], tuple[str, int]] = {}

    async def resolve(self, name: str) -> list[IPAddress]:
        if name not in self.names:
            raise Unresolved(name)
        return self.names[name]

    async def service(self, name: str, port: int) -> tuple[str, int] | None:
        return self.services.get((name, port))


async def echo(stream: anyio.abc.SocketStream) -> None:
    async with stream:
        async for chunk in stream:
            await stream.send(b"echo:" + chunk)


@pytest.fixture
async def servers() -> AsyncIterator[tuple[int, int]]:
    """An echo server and a proxy in front of it: their ports."""
    resolver = FakeResolver({"echo.lab": [ipaddress.IPv4Address("127.0.0.1")]})
    async with anyio.create_task_group() as group:
        listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
        group.start_soon(listener.serve, echo)
        echo_port = listener.extra(anyio.abc.SocketAttribute.local_port)
        proxy_port = await group.start(serve, "127.0.0.1", 0, Resolving(resolver))
        yield proxy_port, echo_port
        group.cancel_scope.cancel()


async def socks_connect(proxy: int, address: bytes, port: int) -> tuple[anyio.abc.SocketStream, bytes]:
    stream = await anyio.connect_tcp("127.0.0.1", proxy)
    await stream.send(bytes([5, 1, 0]))
    assert await stream.receive(2) == bytes([5, 0])
    await stream.send(bytes([5, 1, 0]) + address + port.to_bytes(2, "big"))
    return stream, await stream.receive(10)


async def test_connect_by_name_carries_bytes(servers: tuple[int, int]) -> None:
    proxy, echo_port = servers
    stream, answer = await socks_connect(proxy, bytes([3, 8]) + b"echo.lab", echo_port)
    async with stream:
        assert answer[1] == Reply.SUCCEEDED
        await stream.send(b"hi")
        assert await stream.receive() == b"echo:hi"


async def test_connect_by_address(servers: tuple[int, int]) -> None:
    proxy, echo_port = servers
    stream, answer = await socks_connect(proxy, bytes([1]) + socket.inet_aton("127.0.0.1"), echo_port)
    async with stream:
        assert answer[1] == Reply.SUCCEEDED


async def test_unknown_name_is_host_unreachable(servers: tuple[int, int]) -> None:
    proxy, echo_port = servers
    stream, answer = await socks_connect(proxy, bytes([3, 9]) + b"other.lab", echo_port)
    async with stream:
        assert answer[1] == Reply.HOST_UNREACHABLE


async def test_closed_port_is_refused(servers: tuple[int, int]) -> None:
    proxy, _ = servers
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    stream, answer = await socks_connect(proxy, bytes([1]) + socket.inet_aton("127.0.0.1"), port)
    async with stream:
        assert answer[1] == Reply.CONNECTION_REFUSED


def test_reply_head_and_request_bytes() -> None:
    assert protocol.connect_request(protocol.Request("a.lab", 80)) == bytes([5, 1, 0, 3, 5]) + b"a.lab" + b"\x00P"
    address = ipaddress.IPv4Address("10.0.0.1")
    assert protocol.connect_request(protocol.Request(address, 443)) == bytes([5, 1, 0, 1, 10, 0, 0, 1, 1, 187])
    assert protocol.parse_reply_head(bytes([5, 4, 0, 1])) == (Reply.HOST_UNREACHABLE, AddressType.IPV4)


def test_lab_holds_its_names_and_networks() -> None:
    lab = Lab(("cluster.local",), (ipaddress.ip_network("10.200.0.0/24"),))
    assert lab.holds("web.default.svc.cluster.local")
    assert lab.holds("10.200.0.7")
    assert lab.holds(ipaddress.IPv4Address("10.200.0.7"))
    assert not lab.holds("example.com")
    assert not lab.holds("local")
    assert not lab.holds(ipaddress.IPv4Address("10.201.0.1"))


class Counting:
    """The router's connector, counting what reaches it."""

    def __init__(self, inner: Resolving) -> None:
        self.inner = inner
        self.seen: list[str] = []

    async def open(self, request: protocol.Request) -> anyio.abc.ByteStream:
        self.seen.append(str(request.host))
        return await self.inner.open(request)


@pytest.fixture
async def split() -> AsyncIterator[tuple[int, int, Counting]]:
    """An echo server, a router-like proxy that resolves `echo.lab`, and a
    split proxy in front: the split proxy's port, the echo's, the router's log."""
    router = Counting(Resolving(FakeResolver({"echo.lab": [ipaddress.IPv4Address("127.0.0.1")]})))
    async with anyio.create_task_group() as group:
        listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
        group.start_soon(listener.serve, echo)
        echo_port = listener.extra(anyio.abc.SocketAttribute.local_port)
        router_port = await group.start(serve, "127.0.0.1", 0, router)
        lab = Lab(("lab",), (ipaddress.ip_network("127.0.0.1/32"),))
        port = await group.start(serve, "127.0.0.1", 0, Split(lab, ("127.0.0.1", router_port)))
        yield port, echo_port, router
        group.cancel_scope.cancel()


async def test_split_sends_a_lab_name_to_the_router(split: tuple[int, int, Counting]) -> None:
    proxy, echo_port, router = split
    stream, answer = await socks_connect(proxy, bytes([3, 8]) + b"echo.lab", echo_port)
    async with stream:
        assert answer[1] == Reply.SUCCEEDED
        await stream.send(b"hi")
        assert await stream.receive() == b"echo:hi"
    assert router.seen == ["echo.lab"]


async def test_split_sends_a_lab_address_to_the_router(split: tuple[int, int, Counting]) -> None:
    proxy, echo_port, router = split
    stream, answer = await socks_connect(proxy, bytes([1]) + socket.inet_aton("127.0.0.1"), echo_port)
    async with stream:
        assert answer[1] == Reply.SUCCEEDED
    assert router.seen == ["127.0.0.1"]


async def test_split_connects_the_rest_itself(split: tuple[int, int, Counting]) -> None:
    proxy, echo_port, router = split
    stream, answer = await socks_connect(proxy, bytes([3, 9]) + b"localhost", echo_port)
    async with stream:
        assert answer[1] == Reply.SUCCEEDED
        await stream.send(b"hi")
        assert await stream.receive() == b"echo:hi"
    assert router.seen == []


async def test_split_passes_the_routers_refusal_on(split: tuple[int, int, Counting]) -> None:
    proxy, echo_port, router = split
    stream, answer = await socks_connect(proxy, bytes([3, 9]) + b"other.lab", echo_port)
    async with stream:
        assert answer[1] == Reply.HOST_UNREACHABLE
    assert router.seen == ["other.lab"]


class Refusing(Resolver):
    """A server that answers A and refuses AAAA, as dnsmasq does with `address=` and no upstream."""

    def __init__(self, answers: dict[str, list[IPAddress]]) -> None:
        super().__init__({"lab": ["10.0.2.15"]})
        self.answers = answers

    async def _query(self, servers: Sequence[str], name: str, rdtype: str) -> list[IPAddress]:
        del servers, name
        if rdtype not in self.answers:
            raise dns.resolver.NoNameservers
        return self.answers[rdtype]


async def test_a_refused_type_keeps_the_other_types_addresses() -> None:
    found = await Refusing({"A": [ipaddress.IPv4Address("10.0.2.15")]}).resolve("web.lab")
    assert found == [ipaddress.IPv4Address("10.0.2.15")]


async def test_a_name_no_type_answers_says_why() -> None:
    with pytest.raises(Unresolved, match=r"web\.lab: All nameservers failed"):
        await Refusing({}).resolve("web.lab")


async def test_a_service_record_sends_the_connection_to_its_target_and_port() -> None:
    """https://alt.lab/ reaches echo.lab on its own port, as a browser reaches a Service's named port."""
    resolver = FakeResolver({"echo.lab": [ipaddress.IPv4Address("127.0.0.1")]})
    async with anyio.create_task_group() as group:
        listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
        group.start_soon(listener.serve, echo)
        resolver.services[("alt.lab", 443)] = ("echo.lab", listener.extra(anyio.abc.SocketAttribute.local_port))
        proxy = await group.start(serve, "127.0.0.1", 0, Resolving(resolver))
        stream, answer = await socks_connect(proxy, bytes([3, 7]) + b"alt.lab", 443)
        async with stream:
            assert answer[1] == Reply.SUCCEEDED
            await stream.send(b"hi")
            assert await stream.receive() == b"echo:hi"
        group.cancel_scope.cancel()


def test_the_record_with_the_lowest_priority_and_then_the_highest_weight_wins() -> None:
    records = [Service(20, 100, 1, "late.lab"), Service(10, 1, 2, "light.lab"), Service(10, 5, 3, "heavy.lab")]
    assert pick(records) == ("heavy.lab", 3)
    assert pick([Service(0, 0, 443, "")]) is None, "a target of `.` says the service is not there"
    assert pick([]) is None


class Services(Resolver):
    """SRV answers by owner name; any other owner fails as a server that does not know it."""

    def __init__(self, records: dict[str, list[Service]]) -> None:
        super().__init__({"lab": ["10.0.2.15"]})
        self.records = records
        self.asked: list[str] = []

    async def _services(self, servers: Sequence[str], owner: str) -> list[Service]:
        del servers
        self.asked.append(owner)
        if owner not in self.records:
            raise dns.resolver.NXDOMAIN
        return self.records[owner]


async def test_only_web_ports_of_lab_names_ask_for_a_service_record() -> None:
    resolver = Services({"_https._tcp.shop.lab": [Service(0, 0, 8443, "shop.lab")]})
    assert await resolver.service("shop.lab", 443) == ("shop.lab", 8443)
    assert await resolver.service("shop.lab", 80) is None, "no `_http._tcp` record: the request's own port"
    assert await resolver.service("shop.lab", 5432) is None
    assert await resolver.service("example.com", 443) is None
    assert resolver.asked == ["_https._tcp.shop.lab", "_http._tcp.shop.lab"]


ZONE = """{
  "domains": ["lab.test"],
  "hosts": {"web.lab.test": ["10.0.2.15"], "shop.lab.test": ["10.0.2.15"]},
  "services": {"_https._tcp.shop.lab.test": [{"priority": 0, "weight": 0, "port": 8443, "target": "shop.lab.test"}]}
}"""


async def test_the_zone_answers_its_names_with_no_server() -> None:
    resolver = Resolver({}, zone=Zone.from_json(ZONE))
    assert await resolver.resolve("WEB.lab.test.") == [ipaddress.IPv4Address("10.0.2.15")]
    with pytest.raises(Unresolved, match=r"nosuch\.lab\.test: no such name in the lab"):
        await resolver.resolve("nosuch.lab.test")
    with pytest.raises(Unresolved, match="under no domain"):
        await resolver.resolve("example.com")


async def test_the_zone_answers_service_records_for_web_ports() -> None:
    resolver = Resolver({}, zone=Zone.from_json(ZONE))
    assert await resolver.service("shop.lab.test", 443) == ("shop.lab.test", 8443)
    assert await resolver.service("web.lab.test", 443) is None, "no record: the request's own port"
    assert await resolver.service("shop.lab.test", 5432) is None


async def test_a_zone_name_reaches_its_port_through_the_proxy() -> None:
    """https://shop.lab.test/ reaches the guest's 8443 from the zone alone, as a browser would."""
    async with anyio.create_task_group() as group:
        listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
        group.start_soon(listener.serve, echo)
        port = listener.extra(anyio.abc.SocketAttribute.local_port)
        zone = Zone(
            frozenset({"lab.test"}),
            {"shop.lab.test": [ipaddress.IPv4Address("127.0.0.1")]},
            {"_https._tcp.shop.lab.test": [Service(0, 0, port, "shop.lab.test")]},
        )
        proxy = await group.start(serve, "127.0.0.1", 0, Resolving(Resolver({}, zone=zone)))
        stream, answer = await socks_connect(proxy, bytes([3, 13]) + b"shop.lab.test", 443)
        async with stream:
            assert answer[1] == Reply.SUCCEEDED
            await stream.send(b"hi")
            assert await stream.receive() == b"echo:hi"
        group.cancel_scope.cancel()


@pytest.mark.parametrize("text", ["[]", '{"hosts": {"a.lab.test": ["not an address"]}}', '{"services": {"x": [{}]}}'])
def test_a_malformed_zone_is_refused(text: str) -> None:
    with pytest.raises((ValueError, KeyError)):
        Zone.from_json(text)
