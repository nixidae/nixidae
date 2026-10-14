"""A plan document, turned into derivations by `graph.nix`, evaluated by `nix eval`.

**This is the mode that EnsurePath makes possible.** `lib/modes/nix-eval.nix`
attaches the string context with `builtins.storePath` and
`builtins.appendContext`, which `builder-rpc-v0` refuses and recursive-nix
allows. So the planner needs a stock `nix`, and no huggorm and no renderer.
The evaluator writes each derivation through the build's daemon socket as it
instantiates it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def nixpkgs_arguments() -> list[str]:
    """nixpkgs for `graph.nix`, when the mode is `recursive-nixpkgs`."""
    nixpkgs = os.environ.get("PYDD_NIXPKGS")
    if not nixpkgs:
        return []
    return [
        *("--argstr", "nixpkgs", nixpkgs),
        *("--argstr", "overlays", os.environ.get("PYDD_NIXPKGS_OVERLAYS", "[]")),
        *("--argstr", "config", os.environ.get("PYDD_NIXPKGS_CONFIG", "{}")),
    ]


def submit_nix_cli(document: dict[str, Any]) -> str:
    """Evaluate `graph.nix` over `document` with `nix eval`, and name the root `.drv`."""
    path = Path.cwd() / "document.json"
    path.write_text(json.dumps(document))
    result = subprocess.run(
        [
            "nix",
            "eval",
            "--raw",
            "--file",
            os.environ["PYDD_NIX_EVAL"],
            "--argstr",
            "document",
            str(path),
            "--argstr",
            "graph",
            os.environ["PYDD_GRAPH_NIX"],
            *nixpkgs_arguments(),
            "drvPath",
        ],
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    )
    root_drv = result.stdout.strip()
    print(f"==> nix eval wrote {root_drv}", file=sys.stderr)
    return root_drv
