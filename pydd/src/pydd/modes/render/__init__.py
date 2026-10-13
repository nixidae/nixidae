"""A plan document, turned into derivations in Python, as `graph.nix` makes them."""

from ._render import Graph, render

__all__ = ["Graph", "render"]
