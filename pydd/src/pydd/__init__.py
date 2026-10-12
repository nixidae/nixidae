"""Dynamic derivations, planned in Python.

A planner is a ``builder-rpc-v0`` derivation. It decides at build time what
Nix builds next: it writes a graph of derivations through the restricted
store socket and submits the root, and ``builtins.outputOf`` builds the graph.

The core is standard library only, so a planner runs under a bare
``python3``. It writes derivations through the ``nix`` CLI, or through
huggorm when the bindings are on the path.
"""

from __future__ import annotations

from ._derivation import Derivation, Fixed, Floating, InputAddressed, Method, Output
from ._hash import downstream_placeholder, nix32, output_path_name, placeholder
from ._planner import Node, Planner
from ._writer import CliWriter, HuggormWriter, Writer

__all__ = [
    "CliWriter",
    "Derivation",
    "Fixed",
    "Floating",
    "HuggormWriter",
    "InputAddressed",
    "Method",
    "Node",
    "Output",
    "Planner",
    "Writer",
    "downstream_placeholder",
    "nix32",
    "output_path_name",
    "placeholder",
]
