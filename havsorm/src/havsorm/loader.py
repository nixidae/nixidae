"""What Salt's loader gives a havsorm module, and what a state gives back, typed.

The loader sets `__opts__`, `__grains__` and `__salt__` on a module after
the import. A module declares the ones it reads, with these types:

    __opts__: Opts
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final, TypeAlias, TypedDict

# Salt's configuration: any key a user writes, any YAML value.
Opts: TypeAlias = dict[str, Any]
# A custom grain may hold any value.
Grains: TypeAlias = dict[str, Any]
# Salt's execution modules declare no types.
Functions: TypeAlias = dict[str, Callable[..., Any]]


class StateReturn(TypedDict):
    """What a state function returns to Salt."""

    name: str
    result: bool | None
    """None: `test=True`, and the state would change something."""
    # A state reports any value it changed.
    changes: dict[str, Any]
    comment: str


DEFAULT_EVAL_STORE: Final = "dummy://"
DEFAULT_STORE: Final = "auto"


@dataclass(frozen=True, slots=True)
class Config:
    """The `havsorm:` section of the minion or master config."""

    eval_store: str = DEFAULT_EVAL_STORE
    """The store an evaluation may read and write."""
    store: str = DEFAULT_STORE
    """The store the states change."""
    nix_settings: dict[str, str] = field(default_factory=dict[str, str])
    """nix.conf settings for this process."""
    warm: bool | None = None
    """Keep the evaluator between renders. None leaves the choice to the caller."""

    @classmethod
    def from_opts(cls, opts: Opts) -> Config:
        section: dict[str, Any] = opts.get("havsorm") or {}
        settings: dict[str, Any] = section.get("nix_settings") or {}
        warm = section.get("warm")
        return cls(
            eval_store=str(section.get("eval_store", DEFAULT_EVAL_STORE)),
            store=str(section.get("store", DEFAULT_STORE)),
            nix_settings={str(name): str(value) for name, value in settings.items()},
            warm=None if warm is None else bool(warm),
        )
