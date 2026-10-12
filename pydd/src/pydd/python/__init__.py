"""Python environments, resolved by ``packaging`` and built as a graph.

``nix/python.nix`` makes the planner. It hands over a lock file with one
fetch per artefact, and this package decides which of them the target needs
and what the graph over them is: one node per wheel, sdist and editable
project, and one for the environment.
"""

from __future__ import annotations

from .._planner import Planner
from ._graph import GraphWriter
from ._resolve import Artifact, Editable, Plan, Target, resolve

__all__ = ["Artifact", "Editable", "GraphWriter", "Plan", "Target", "main", "resolve"]


def main() -> None:
    """The planner of ``mkEnvironment``."""
    planner = Planner.from_environ()
    menu = planner.menu
    target = Target.cpython(menu["pythonVersion"], menu["platform"], menu["sysPlatform"])
    plan = resolve(
        [Artifact.from_json(row) for row in menu["artifacts"]],
        menu["roots"],
        menu["installer"],
        [
            Editable(name, spec["source"], spec["root"])
            for name, spec in sorted(menu["editables"].items())
        ],
        target,
    )
    writer = GraphWriter(planner, menu["tools"], menu["scripts"], menu["pythonVersion"])
    planner.submit(writer.write(plan, menu["name"]))
