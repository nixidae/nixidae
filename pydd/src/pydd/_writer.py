"""The two ways a planner reaches the store: the ``nix`` CLI, or huggorm.

Both take the same JSON and call the same libstore code. The CLI needs
nothing but a ``nix`` on ``PATH``; huggorm skips a process per derivation.
"""

from __future__ import annotations

import json
import os
import posixpath
import subprocess
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

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


class OutWriter(HuggormWriter):
    """A planner on released Nix, which has no ``builder-rpc-v0``.

    Nix reads one derivation, as ATerm, from ``$out``. Each derivation the
    planner adds goes to a writable ``dummy://`` in memory, which holds no
    input and needs none, so a planner is the same code for both protocols.
    Only the root leaves it, and only when every path the root names is
    already in the sandbox: Nix registers no reference to anything else, and
    the root then silently becomes another derivation.
    """

    def __init__(self, uri: str = "dummy://?read-only=false", present: Callable[[str], bool] = os.path.exists) -> None:
        super().__init__(uri)
        self.present = present

    def submit(self, path: str, output: str) -> None:
        if output != "out":
            raise ValueError(f"a planner on released Nix writes only its output 'out', not {output!r}")
        store_path = self.store.parse_store_path(path)
        drv = self.store.read_derivation(store_path)
        store_dir = posixpath.dirname(path)
        named = [str(self.store.print_store_path(src)) for src in drv.input_srcs()]
        named += [f"{store_dir}/{base}" for base in drv.input_drvs()]
        missing = sorted(name for name in named if not self.present(name))
        if missing:
            raise RuntimeError(
                f"{path} names {', '.join(missing)}, which this build does not have. "
                "On released Nix a planner writes one derivation, so every other one "
                "it names must be an input of the planner, made before the build."
            )
        aterm: bytes = self.store.get_fs_accessor(store_path, False).read_file("")
        with open(os.environ["out"], "wb") as sink:  # noqa: SIM112 - Nix names it `out`
            sink.write(aterm)


WRITERS = {"cli": CliWriter, "huggorm": HuggormWriter}
