"""What to build for a review, within a build-time budget.

A set of changed derivations that holds every changed input of its members
builds exactly itself, so its estimate is the sum over it. The planner
grows such a set depth by depth, cheapest first, until the budget is
spent. Part of a depth gets in when the whole of it does not fit. A
derivation with no time counts as `untimed_seconds`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from .bundle import Bundle, Derivation

BUDGET_SECONDS: Final = 3600
UNTIMED_SECONDS: Final = 60
"""A guess, not a measurement."""
STORE_DIR: Final = "/nix/store"


class Plan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", use_attribute_docstrings=True)

    budget: int
    untimed_seconds: int
    depth: int
    """The deepest longest-path depth `builds` holds in full; 0 when it holds none."""
    estimate: int
    """Build seconds of `builds`, untimed derivations at `untimed_seconds`."""
    untimed: int
    """Derivations in `builds` with no time."""
    builds: list[str]
    installables: list[str]
    """`<store>/<drv>^*` for each of `builds`, for `nix build`."""
    skipped: list[str]
    """Changed derivations left out: over the budget, or needing one that is."""
    total_estimate: int
    """What building every changed derivation would cost."""


def cost(d: Derivation, untimed_seconds: int) -> int:
    return d.time.estimate if d.time is not None else untimed_seconds


def order(bundle: Bundle, untimed_seconds: int = UNTIMED_SECONDS) -> list[Derivation]:
    """Shallowest first, then cheapest: every changed input comes before what needs it."""
    return sorted(bundle.derivations, key=lambda d: (d.depth, cost(d, untimed_seconds), d.drv))


def plan(
    bundle: Bundle,
    *,
    budget: int = BUDGET_SECONDS,
    untimed_seconds: int = UNTIMED_SECONDS,
    store_dir: str = STORE_DIR,
) -> Plan:
    chosen: set[str] = set()
    spent = 0
    for d in order(bundle, untimed_seconds):
        price = cost(d, untimed_seconds)
        if spent + price <= budget and chosen.issuperset(d.changed_inputs):
            chosen.add(d.drv)
            spent += price

    builds = [d for d in bundle.derivations if d.drv in chosen]
    skipped = [d for d in bundle.derivations if d.drv not in chosen]
    complete = min(d.depth for d in skipped) - 1 if skipped else max((d.depth for d in builds), default=0)
    return Plan(
        budget=budget,
        untimed_seconds=untimed_seconds,
        depth=complete,
        estimate=spent,
        untimed=sum(1 for d in builds if d.time is None),
        builds=[d.drv for d in builds],
        installables=[f"{store_dir}/{d.drv}^*" for d in builds],
        skipped=[d.drv for d in skipped],
        total_estimate=sum(cost(d, untimed_seconds) for d in bundle.derivations),
    )
