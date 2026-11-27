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
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    def __init__(self, domains: Mapping[str, Sequence[str]], timeout: float = 5.0) -> None:
        self.domains = {domain.lower().rstrip("."): list(servers) for domain, servers in domains.items()}
        self.timeout = timeout

    async def resolve(self, name: str) -> list[IPAddress]:
        servers = servers_for(name, self.domains)
        if servers is None:
            raise Unresolved(f"{name}: under no domain this proxy resolves ({', '.join(sorted(self.domains))})")
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
