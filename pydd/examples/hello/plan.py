"""Two nodes that do not exist before the plan runs: the root reads the leaf."""

from __future__ import annotations

from pydd import Derivation, Planner

planner = Planner.from_environ()
bash = planner.menu["bash"]


def node(name: str, script: str, drvs: dict[str, list[str]] | None = None) -> Derivation:
    return Derivation(
        name=name,
        system=planner.system,
        builder=f"{bash}/bin/sh",
        args=["-c", script],
        srcs=[bash],
        drvs=drvs or {},
    )


leaf = planner.add(node("hello-leaf", 'echo "hello from a node the plan wrote" > $out'))
root = planner.add(node("hello", f'read -r line < {leaf.output()}; echo "root read: $line" > $out', leaf.needs()))
planner.submit(root)
