"""Store paths a run keeps alive while it runs.

A run reads its spec once and boots a template's image long after. A
garbage collection in between found no root for either, and deleted the
image under a cloud test (nixidae/nixidae#1511).

The root is the spec file, held open. Nix and pynixd count each file a
process holds open as a runtime root, and keep its closure; the spec's
closure holds every image, kernel and phase the run can use. The root
ends with the process, however it ends, and leaves nothing to clean up.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from typing import BinaryIO


class Roots:
    """The files this run holds open, so their closures stay in the store."""

    def __init__(self) -> None:
        self._held: dict[Path, BinaryIO] = {}

    def hold(self, path: Path) -> None:
        """Keep `path` and its closure until `close`, or the process ends."""
        target = path.resolve()
        if target not in self._held:
            self._held[target] = target.open("rb")

    @property
    def held(self) -> list[Path]:
        return sorted(self._held)

    def close(self) -> None:
        for handle in self._held.values():
            handle.close()
        self._held.clear()
