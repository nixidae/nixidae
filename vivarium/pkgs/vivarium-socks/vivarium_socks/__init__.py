"""A SOCKS5 proxy into a vivarium lab: the host reaches lab addresses and
lab names through one forward to the router."""

from __future__ import annotations

from .resolve import Resolver as Resolver
from .server import serve as serve
