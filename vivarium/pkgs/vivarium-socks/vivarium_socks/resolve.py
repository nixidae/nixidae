"""Names to addresses, each domain from its own servers.

The proxy resolves inside the lab: `cluster.local` from a cluster's DNS,
another domain from another lab's. A name under no configured domain has
no answer: the router itself has no resolver, and the lab's servers know
only their own names.

A connection to a lab name on port 443 or 80 asks first for the name's
SRV record (RFC 2782), `_https._tcp.<name>` or `_http._tcp.<name>`, and
goes to its target and port: Kubernetes publishes one for each named
Service port, so https://shop.default.svc.cluster.local/ reaches a
Service whose `https` port is 8443. No record leaves the request as it
came. nixidae/nixidae#1656.

A `Zone` holds the names the lab declares itself (`vivarium.lab`): each
guest's address and its ports' SRV records, built at evaluation and
answered with no DNS server. nixidae/nixidae#1669.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

import dns.asyncresolver
import dns.exception
import dns.name
import dns.resolver

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

SERVICES: Final[Mapping[int, str]] = {443: "_https._tcp", 80: "_http._tcp"}
"""The SRV owner prefix a connection to each port looks up."""


class Unresolved(Exception):
    """No address for a name."""


@dataclass(frozen=True, slots=True)
class Service:
    """One SRV record."""

    priority: int
    weight: int
    port: int
    target: str
    """Without the trailing dot; `.` (empty here) is "no such service" (RFC 2782)."""


class NameResolver(Protocol):
    async def resolve(self, name: str) -> list[IPAddress]: ...

    async def service(self, name: str, port: int) -> tuple[str, int] | None:
        """Where a connection to *name* on *port* goes instead, from its SRV record."""
        ...


@dataclass(frozen=True, slots=True)
class Zone:
    """Names the lab declares, answered without a DNS server: a guest's address
    and the SRV records of its ports, built when the run is evaluated."""

    domains: frozenset[str] = frozenset()
    hosts: Mapping[str, Sequence[IPAddress]] = field(default_factory=dict)
    services: Mapping[str, Sequence[Service]] = field(default_factory=dict)
    """By owner, such as `_https._tcp.web.lab.test`."""

    def holds(self, name: str) -> bool:
        labels = _labels(name)
        return any(".".join(labels[start:]) in self.domains for start in range(len(labels)))

    @classmethod
    def from_json(cls, text: str) -> Zone:
        """`{"domains": [...], "hosts": {name: [address]}, "services": {owner: [{priority, weight, port, target}]}}`.
        Raises ValueError."""
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("a zone is a JSON object")
        return cls(
            frozenset(_name(each) for each in raw.get("domains", [])),
            {
                _name(name): [ipaddress.ip_address(each) for each in found]
                for name, found in raw.get("hosts", {}).items()
            },
            {
                _name(owner): [
                    Service(int(each["priority"]), int(each["weight"]), int(each["port"]), _name(each["target"]))
                    for each in records
                ]
                for owner, records in raw.get("services", {}).items()
            },
        )


def _labels(name: str) -> list[str]:
    return _name(name).split(".")


def _name(name: str) -> str:
    return name.lower().rstrip(".")


def servers_for(name: str, domains: Mapping[str, Sequence[str]]) -> Sequence[str] | None:
    """The servers of the longest configured domain that holds *name*."""
    labels = name.lower().rstrip(".").split(".")
    for start in range(len(labels)):
        suffix = ".".join(labels[start:])
        if suffix in domains:
            return domains[suffix]
    return None


def pick(records: Sequence[Service]) -> tuple[str, int] | None:
    """The record to use: lowest priority, then highest weight. RFC 2782 picks among equal
    priorities at random by weight; one proxy connecting once a request needs no spread."""
    usable = [record for record in records if record.target]
    if not usable:
        return None
    best = min(usable, key=lambda record: (record.priority, -record.weight))
    return best.target, best.port


class Resolver:
    def __init__(self, domains: Mapping[str, Sequence[str]], timeout: float = 5.0, zone: Zone | None = None) -> None:
        self.domains = {domain.lower().rstrip("."): list(servers) for domain, servers in domains.items()}
        self.timeout = timeout
        self.zone = zone or Zone()

    async def resolve(self, name: str) -> list[IPAddress]:
        if self.zone.holds(name):
            if declared := self.zone.hosts.get(_name(name)):
                return list(declared)
            raise Unresolved(f"{name}: no such name in the lab ({', '.join(sorted(self.zone.domains))})")
        servers = servers_for(name, self.domains)
        if servers is None:
            known = sorted({*self.domains, *self.zone.domains})
            raise Unresolved(f"{name}: under no domain this proxy resolves ({', '.join(known)})")
        found: list[IPAddress] = []
        failed: dns.exception.DNSException | None = None
        # One type's failure does not hide the other's answer: dnsmasq with
        # `address=/name/v4` and no upstream refuses AAAA (measured, checks.browser).
        for rdtype in ("A", "AAAA"):
            try:
                found += await self._query(servers, name, rdtype)
            except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
                continue
            except dns.exception.DNSException as error:
                failed = failed or error
        if not found:
            raise Unresolved(f"{name}: {failed or 'no address'}") from failed
        return found

    async def service(self, name: str, port: int) -> tuple[str, int] | None:
        prefix = SERVICES.get(port)
        if prefix is not None and self.zone.holds(name):
            return pick(self.zone.services.get(f"{prefix}.{_name(name)}", ()))
        servers = servers_for(name, self.domains)
        if prefix is None or servers is None:
            return None
        try:
            return pick(await self._services(servers, f"{prefix}.{name.rstrip('.')}"))
        except dns.exception.DNSException:
            # No record, or a server that will not say: the request goes where it asked.
            return None

    async def _query(self, servers: Sequence[str], name: str, rdtype: str) -> list[IPAddress]:
        answer = await self._resolver(servers).resolve(name, rdtype)
        return [ipaddress.ip_address(record.to_text()) for record in answer]

    async def _services(self, servers: Sequence[str], owner: str) -> list[Service]:
        answer = await self._resolver(servers).resolve(owner, "SRV")
        return [
            Service(
                record.priority,
                record.weight,
                record.port,
                "" if record.target == dns.name.root else record.target.to_text(omit_final_dot=True),
            )
            for record in answer
        ]

    def _resolver(self, servers: Sequence[str]) -> dns.asyncresolver.Resolver:
        resolver = dns.asyncresolver.Resolver(configure=False)
        resolver.nameservers = list(servers)
        resolver.lifetime = self.timeout
        return resolver
