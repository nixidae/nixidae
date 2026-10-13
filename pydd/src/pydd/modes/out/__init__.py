"""Released Nix: a planner writes one derivation, so other planners write the rest.

A node whose shape only a build can decide is the root of a planner that the
host made (`lib/python/planner.nix`): each local project, in each form, and
the environment under an application. This planner makes the same plan as
every other mode, and then puts a node of kind `planned` in the place of
each node that another planner writes. `PYDD_ONLY` names the one local node
that this planner writes itself, when it is such a planner.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ...python._plan import Plan


def root(plan: Plan, inputs: dict[str, Any], name: str, application: str | Sequence[str] | None) -> str:
    """Put in the planned nodes, and name the node that this planner submits."""
    planned: dict[str, dict[str, str]] = inputs.get("planned", {})
    only = os.environ.get("PYDD_ONLY")
    for key, node in plan.local_nodes.items():
        if key in planned and key != only:
            plan.replace(node, plan.planned_entry(node, planned[key]))
    if only is not None:
        if only not in plan.local_nodes:
            raise RuntimeError(f"PYDD_ONLY is {only!r}, and the plan has no local project of that key")
        return plan.local_nodes[only]

    submitted = plan.root(name, application=application)
    venv_planner: dict[str, str] | None = inputs.get("venvPlanner")
    if application is not None and venv_planner is not None:
        plan.replace(f"{name}-env", plan.planned_entry(f"{name}-env", venv_planner))
    return submitted
