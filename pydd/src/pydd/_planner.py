"""What a planner reads from its environment, and how it registers a graph."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any

from ._derivation import Derivation, Floating
from ._hash import downstream_placeholder
from ._writer import WRITERS, OutWriter, Writer


@dataclass(frozen=True, slots=True)
class Node:
    """A derivation that the planner has written."""

    path: str
    drv: Derivation

    def output(self, name: str = "out") -> str:
        """What another node writes for output ``name`` of this one."""
        if not isinstance(self.drv.outputs.get(name), Floating):
            raise ValueError(f"{self.drv.name}: output {name!r} is not floating")
        return downstream_placeholder(self.path, name)

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
        if os.environ.get("PYDD_PROTOCOL") == "out":
            writer: Writer = OutWriter()
        else:
            writer = WRITERS[os.environ.get("PYDD_WRITER", "cli")]()
        return cls(writer, os.environ["PYDD_SYSTEM"], json.loads(os.environ.get("PYDD_MENU", "null")))

    def add(self, drv: Derivation) -> Node:
        node = Node(self.writer.add(drv), drv)
        print(f"pydd: added {node.path}", file=sys.stderr)
        return node

    def submit(self, root: Node) -> None:
        """Make ``root`` the result of the planner."""
        self.writer.submit(root.path, "out")
        print(f"pydd: submitted {root.path}", file=sys.stderr)
