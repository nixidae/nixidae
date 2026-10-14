"""`pydd-drv graph` against the paths that pydd and Nix compute one at a time."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from test_against_nix import drv_path

from pydd import Derivation, downstream_placeholder

TOOL = os.environ["PYDD_DRV"]


def node(name: str, **fields: Any) -> Derivation:
    return Derivation(name=name, system="x86_64-linux", builder="/bin/sh", **fields)


def graph(
    derivations: dict[str, dict[str, Any]], root: str, *flags: str, **env: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [TOOL, "graph", *flags],
        input=json.dumps({"derivations": derivations, "root": root}),
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        check=False,
    )


def symbolic(drv: Derivation, **drvs: list[str]) -> dict[str, Any]:
    value = drv.to_json()
    value["inputs"]["drvs"] = {key: {"outputs": outputs, "dynamicOutputs": {}} for key, outputs in drvs.items()}
    return value


def test_a_chain_gets_the_paths_that_one_add_at_a_time_gets() -> None:
    leaf = node("leaf", args=["-c", "echo leaf > $out"])
    leaf_path = drv_path(leaf)
    mid = node("mid", args=["-c", f"cat {downstream_placeholder(leaf_path)} > $out"], drvs={leaf_path: ["out"]})
    mid_path = drv_path(mid)
    root = node("root.drv", env={"mid": downstream_placeholder(mid_path)}, drvs={mid_path: ["out"]})

    derivations = {
        "leaf": leaf.to_json(),
        "mid": symbolic(node("mid", args=["-c", "cat {{leaf^out}} > $out"]), leaf=["out"]),
        "root": symbolic(node("root.drv", env={"mid": "{{mid^out}}"}), mid=["out"]),
    }
    result = graph(derivations, "root", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"leaf": leaf_path, "mid": mid_path, "root": drv_path(root)}


def test_a_token_that_names_no_node_is_refused() -> None:
    derivations = {"root": node("root.drv", env={"x": "{{nothing^out}}"}).to_json()}
    result = graph(derivations, "root", "--dry-run")
    assert result.returncode == 1
    assert "'nothing'" in result.stderr


def test_out_writes_the_root_as_aterm(tmp_path: Path) -> None:
    root = node("root.drv", args=["-c", "true"])
    out = tmp_path / "out"
    result = graph({"root": root.to_json()}, "root", "--out", out=str(out))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"root": drv_path(root)}
    assert out.read_text().startswith("Derive(")


def test_out_refuses_a_root_that_names_a_node_the_build_lacks(tmp_path: Path) -> None:
    derivations = {
        "leaf": node("leaf").to_json(),
        "root": symbolic(node("root.drv", env={"leaf": "{{leaf^out}}"}), leaf=["out"]),
    }
    result = graph(derivations, "root", "--out", out=str(tmp_path / "out"))
    assert result.returncode == 1
    assert "which this build does not have" in result.stderr
