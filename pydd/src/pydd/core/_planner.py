"""What a planner reads from its environment, and how it registers a graph."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any

from ._derivation import Derivation, Fixed, Floating
from ._hash import downstream_placeholder, fixed_output_path, output_path_name
from ._writer import Writer, writer_from_environ


@dataclass(frozen=True, slots=True)
class Node:
    """A derivation that the planner has written."""

    path: str
    drv: Derivation

    def output(self, name: str = "out") -> str:
        """What another node writes for output ``name`` of this one.

        A floating output has a placeholder, and a fixed one its path.
        """
        match self.drv.outputs.get(name):
            case Floating():
                return downstream_placeholder(self.path, name)
            case Fixed(hash_, method):
                store = os.path.dirname(self.path)
                return fixed_output_path(output_path_name(self.drv.name, name), hash_, method, store)
            case _:
                raise ValueError(f"{self.drv.name}: output {name!r} is neither floating nor fixed")

    def needs(self, *outputs: str) -> dict[str, list[str]]:
        """The ``drvs`` entry of a node that reads ``outputs`` of this one."""
        return {self.path: list(outputs or ("out",))}


class Planner:
    """The planner side of ``lib/planner.nix``.

    A planner registers a graph and cannot build it. It adds each derivation
    leaves first, so every input exists when Nix checks a node, and then
    submits the root. ``builtins.outputOf`` on the planner builds the graph.
    """

    def __init__(self, writer: Writer, system: str, menu: Any) -> None:
        self.writer = writer
        self.system = system
        self.menu = menu

    @classmethod
    def from_environ(cls) -> Planner:
        # `mkPlanner` passes the menu as a file: an environment string over
        # 128 KiB fails `execve`.
        menu = os.environ.get("PYDD_MENU")
        if menu is None and (path := os.environ.get("PYDD_MENUPath")):  # noqa: SIM112 - Nix names it
            with open(path, encoding="utf-8") as handle:
                menu = handle.read()
        return cls(writer_from_environ(), os.environ["PYDD_SYSTEM"], json.loads(menu or "null"))

    def add(self, drv: Derivation) -> Node:
        node = Node(self.writer.add(drv), drv)
        print(f"pydd: added {node.path}", file=sys.stderr)
        return node

    def submit(self, root: Node) -> None:
        """Make ``root`` the result of the planner."""
        self.writer.submit(root.path, "out")
        print(f"pydd: submitted {root.path}", file=sys.stderr)
