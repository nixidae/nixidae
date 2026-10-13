"""hello on released Nix: the root reads a leaf that the host made."""

from __future__ import annotations

from pydd import Derivation, Planner

planner = Planner.from_environ()
bash = planner.menu["bash"]
leaf = planner.menu["leaf"]

root = planner.add(
    Derivation(
        name="released",
        system=planner.system,
        builder=f"{bash}/bin/sh",
        args=["-c", f'read -r line < {leaf["outputs"]["out"]}; echo "root read: $line" > $out'],
        srcs=[bash],
        drvs={leaf["drv"]: ["out"]},
    )
)
planner.submit(root)
