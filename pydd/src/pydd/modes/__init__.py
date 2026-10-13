"""The ways to turn a plan document into derivations, one subpackage each.

`render` writes them in Python. `evaluate` evaluates `graph.nix` in huggorm.
Each mode imports only what it needs, so a planner carries the files of its
own mode and no other.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, Any

from ..core._writer import HuggormWriter

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..core._writer import Writer
    from ..python._plan import Plan


def submit(writer: Writer, document: dict[str, Any]) -> None:
    """Turn `document` into derivations, and submit the root.

    `PYDD_RENDER` names the path. `nix` evaluates `graph.nix`, and needs the
    huggorm writer. `python` renders the same derivations with
    `pydd.modes.render`, through either writer.
    """
    store_dir = os.environ.get("NIX_STORE", "/nix/store")
    match os.environ.get("PYDD_RENDER", "nix"):
        case "nix":
            if not isinstance(writer, HuggormWriter):
                raise RuntimeError("PYDD_RENDER=nix evaluates graph.nix in huggorm, so it needs PYDD_WRITER=huggorm")
            from .evaluate._document import submit_evaluated

            root_drv = submit_evaluated(writer, document, os.environ["PYDD_GRAPH_NIX"], store_dir)
        case "python":
            from .render import render

            root_drv = render(document, writer.add, store_dir)
            print(f"==> the renderer wrote {root_drv}", file=sys.stderr)
        case other:
            raise RuntimeError(f"PYDD_RENDER is {other!r}, and it takes 'nix' or 'python'")

    writer.submit(root_drv, "out")
    print("==> submitted it as output 'out'", file=sys.stderr)


def root(plan: Plan, inputs: dict[str, Any], name: str, application: str | Sequence[str] | None) -> str:
    """The node that the planner of an environment submits.

    On released Nix (`PYDD_PROTOCOL=out`) `pydd.modes.out` decides it, and
    puts in the nodes that other planners write.
    """
    if os.environ.get("PYDD_PROTOCOL") == "out":
        from .out import root as out_root

        return out_root(plan, inputs, name, application)
    return plan.root(name, application=application)
