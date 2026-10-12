"""Python environments, resolved by ``packaging`` and built as a graph.

``lib/python.nix`` makes the planner. It hands over a ``uv.lock`` with one
fetch per artefact, and this package decides which of them the target needs
and what the graph over them is: one node per wheel, sdist and editable
project, and one for the environment.
"""

from __future__ import annotations

from .._planner import Planner
from ._graph import GraphWriter
from ._resolve import Artifact, Editable, Plan, Target, resolve
from ._uvlock import Locked, read

__all__ = [
    "Artifact",
    "Editable",
    "GraphWriter",
    "Locked",
    "Plan",
    "Target",
    "main",
    "read",
    "resolve",
]


def main() -> None:
    """The planner of ``mkEnvironment``."""
    planner = Planner.from_environ()
    menu = planner.menu
    target = Target.cpython(menu["pythonVersion"], menu["platform"], menu["sysPlatform"])
    locked = read(
        menu["packages"],
        menu["fetched"],
        target,
        source=menu["source"],
        groups=menu["groups"],
        extras=menu["extras"],
        build_systems=menu["buildSystems"],
        no_binary=menu["noBinary"],
        editable_roots=menu["editableRoots"],
    )
    plan = resolve(locked.artifacts, locked.roots, menu["installer"], locked.editables, target)
    writer = GraphWriter(planner, menu["tools"], menu["scripts"], target.python_version)
    planner.submit(writer.write(plan, menu["name"]))
