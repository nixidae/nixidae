"""`state.json`: what a named environment's phases keep across its runs.

`vms.state` is a phase's own keys; `vms.previous` is each phase's
outcome in the environment's last run. A phase reads both to decide
whether its work is done (`vms.skip`). Only an environment keeps them,
in `<state>/envs/<name>/state.json`, beside its disks, and `vivarium rm`
deletes them together. Every other run starts empty and writes nothing,
so a check always proves the blank path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import anyio

from . import places

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from .phases import PhaseState

FILE: Final = "state.json"
DISKS: Final = "disks"
"""Each guest's `vivarium.persist` store, by guest name."""
KEYS: Final = "keys"
OUTCOMES: Final = "outcomes"


class KeptError(ValueError):
    pass


@dataclass(slots=True)
class Kept:
    """One run's view of `state.json`. *file* is None outside an environment."""

    file: Path | None = None
    # Any: each phase defines its own keys; they need only be JSON.
    keys: dict[str, Any] = field(default_factory=dict)
    outcomes: dict[str, str] = field(default_factory=dict)
    """The last run's, until this run writes its own at its verdict."""
    _lock: anyio.Lock = field(default_factory=anyio.Lock)
    _saved: str = ""
    """The keys as last written, as JSON."""

    def __post_init__(self) -> None:
        self._saved = json.dumps(self.keys)

    def document(self) -> str:
        """Raises KeptError on a key no JSON holds, after it puts `keys` back
        as they were last saved: in place, since `vms.state` is the same
        dict. The phase that set the value fails; the phases after it, and
        the verdict, write what is good."""
        try:
            keys = json.dumps(self.keys)
        except (TypeError, ValueError) as error:
            bad = sorted(key for key, value in self.keys.items() if not _encodes(value))
            self.keys.clear()
            self.keys.update(json.loads(self._saved))
            raise KeptError(f"vms.state{bad} holds a value JSON cannot: {error}") from error
        self._saved = keys
        return json.dumps({KEYS: self.keys, OUTCOMES: self.outcomes}, indent=2, sort_keys=True) + "\n"

    async def save(self) -> None:
        """Write the keys now: a run that dies later still kept them."""
        if self.file is None:
            return
        async with self._lock:
            partial = anyio.Path(self.file.with_name(f".{FILE}.partial"))
            await partial.write_text(self.document())
            await partial.rename(self.file)

    def finish(self, state: Mapping[str, PhaseState]) -> None:
        """At the verdict: this run's outcomes become the next run's `previous`."""
        if self.file is None:
            return
        self.outcomes = {name: str(value) for name, value in state.items()}
        try:
            document = self.document()
        except KeptError:
            # The phase that set the value failed for it already.
            document = self.document()
        partial = self.file.with_name(f".{FILE}.partial")
        partial.write_text(document)
        partial.rename(self.file)


def _encodes(value: object) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def parse(file: Path, text: str) -> Kept:
    try:
        found = json.loads(text)
    except json.JSONDecodeError as error:
        raise KeptError(f"{file} is no JSON: {error}") from error
    keys = found.get(KEYS) if isinstance(found, dict) else None
    outcomes = found.get(OUTCOMES) if isinstance(found, dict) else None
    if not isinstance(keys, dict) or not isinstance(outcomes, dict):
        raise KeptError(f"{file} is not an object with `{KEYS}` and `{OUTCOMES}`")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in outcomes.items()):
        raise KeptError(f"{file}: `{OUTCOMES}` maps phase names to states")
    return Kept(file, keys, outcomes)


async def load(out: Path) -> Kept:
    """The environment's kept state; empty for a first run and for any
    `--out` that is no environment. A damaged file raises: starting blank
    would redo work on disks that already hold it."""
    if places.environment_name(out) is None:
        return Kept()
    file = out / FILE
    try:
        text = await anyio.Path(file).read_text()
    except FileNotFoundError:
        return Kept(file)
    return parse(file, text)
