"""The caller's side of the engine's contract, for tests.

kappy has no labels or field manager of its own. The suite plays a caller that
chose these, and binds them once so a call reads as the behaviour it tests.
"""

from __future__ import annotations

from functools import partial
from typing import Final

from kappy.apply import (
    apply_and_prune as _apply_and_prune,
    field_manager_for as _field_manager_for,
    prune_selector as _prune_selector,
)
from kappy.directapply import converge_direct as _converge_direct

ENVIRONMENT_LABEL: Final = "ekn.dev/environment"
UNIT_LABEL: Final = "ekn.dev/deployment-unit"
FIELD_MANAGER: Final = "ekn"

apply_and_prune = partial(
    _apply_and_prune, environment_label=ENVIRONMENT_LABEL, unit_label=UNIT_LABEL, field_manager=FIELD_MANAGER
)
converge_direct = partial(
    _converge_direct, environment_label=ENVIRONMENT_LABEL, unit_label=UNIT_LABEL, field_manager=FIELD_MANAGER
)
field_manager_for = partial(_field_manager_for, unit_label=UNIT_LABEL)
prune_selector = partial(_prune_selector, environment_label=ENVIRONMENT_LABEL, unit_label=UNIT_LABEL)
