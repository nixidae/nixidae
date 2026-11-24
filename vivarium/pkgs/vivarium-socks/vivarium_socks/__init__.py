"""A SOCKS5 proxy into a vivarium lab: the host reaches lab addresses and
lab names through one forward to the router."""

from __future__ import annotations

from .resolve import Resolver as Resolver
from .server import Resolving as Resolving, serve as serve
from .split import Lab as Lab, Split as Split
