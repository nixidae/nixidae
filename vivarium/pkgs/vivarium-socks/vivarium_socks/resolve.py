"""Names to addresses, each domain from its own servers.

The proxy resolves inside the lab: `cluster.local` from a cluster's DNS,
another domain from another lab's. A name under no configured domain has
no answer: the router itself has no resolver, and the lab's servers know
only their own names.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence
from typing import Protocol

import dns.asyncresolver
import dns.exception
import dns.resolver

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class Unresolved(Exception):
    """No address for a name."""


class NameResolver(Protocol):
    async def resolve(self, name: str) -> list[IPAddress]: ...


def servers_for(name: str, domains: Mapping[str, Sequence[str]]) -> Sequence[str] | None:
    """The servers of the longest configured domain that holds *name*."""
    labels = name.lower().rstrip(".").split(".")
    for start in range(len(labels)):
        suffix = ".".join(labels[start:])
        if suffix in domains:
            return domains[suffix]
    return None


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

    async def _query(self, servers: Sequence[str], name: str, rdtype: str) -> list[IPAddress]:
        resolver = dns.asyncresolver.Resolver(configure=False)
        resolver.nameservers = list(servers)
        resolver.lifetime = self.timeout
        answer = await resolver.resolve(name, rdtype)
        return [ipaddress.ip_address(record.to_text()) for record in answer]
