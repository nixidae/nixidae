"""The two ways a planner reaches the store: the ``nix`` CLI, or huggorm.

Both take the same JSON and call the same libstore code. The CLI needs
nothing but a ``nix`` on ``PATH``; huggorm skips a process per derivation.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ._derivation import Derivation


class Writer(Protocol):
    def add(self, drv: Derivation) -> str:
        """Write ``drv`` and return its absolute ``.drv`` path."""
        ...

    def submit(self, path: str, output: str) -> None:
        """Register ``path`` as output ``output`` of the build that runs now."""
        ...


class CliWriter:
    """``nix derivation add`` and ``nix store submit-output``."""

    def __init__(self, nix: str = "nix") -> None:
        self.nix = nix

    def add(self, drv: Derivation) -> str:
        result = subprocess.run(
            [self.nix, "derivation", "add"],
            input=json.dumps(drv.to_json()),
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def submit(self, path: str, output: str) -> None:
        subprocess.run([self.nix, "store", "submit-output", path, output], check=True)


class HuggormWriter:
    """``Store.add_derivation`` and ``Store.submit_output``, in process."""

    def __init__(self, uri: str | None = None) -> None:
        # Here, so the CLI writer runs where the bindings are absent.
        import huggorm

        # NIX_CONFIG enables ca-derivations; huggorm reads it only when asked.
        huggorm.load_config()
        self.store: Any = huggorm.Store(uri or os.environ["NIX_REMOTE"])

    def add(self, drv: Derivation) -> str:
        path = self.store.add_derivation(json.dumps(drv.to_json()))
        return str(self.store.print_store_path(path))

    def submit(self, path: str, output: str) -> None:
        self.store.submit_output(self.store.parse_store_path(path), output)


WRITERS = {"cli": CliWriter, "huggorm": HuggormWriter}
