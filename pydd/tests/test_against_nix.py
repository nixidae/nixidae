"""pydd's JSON and placeholders, held to what Nix makes of the same input."""

from __future__ import annotations

import base64
import json
import os
import subprocess
from typing import Any

import pytest

from pydd import Derivation, Fixed, Floating, downstream_placeholder, placeholder

FIXTURE: dict[str, Any] = json.loads(os.environ["PYDD_FIXTURE"])


def drv_path(drv: Derivation) -> str:
    """The path Nix gives ``drv``, computed without a store to write to."""
    result = subprocess.run(
        ["nix", "derivation", "add", "--dry-run", "--store", "dummy://"],
        input=json.dumps(drv.to_json()),
        stdout=subprocess.PIPE,
        text=True,
        check=True,
        env={
            **os.environ,
            "NIX_CONFIG": "extra-experimental-features = nix-command ca-derivations dynamic-derivations",
        },
    )
    return result.stdout.strip()


def from_attrs(attrs: dict[str, Any], **outputs: Any) -> Derivation:
    """What ``builtins.derivation`` makes of ``attrs``, spelled for pydd."""
    # `derivation` keeps neither of these in the environment.
    dropped = {"args", "__contentAddressed"}
    env = {key: value for key, value in attrs.items() if key not in dropped}
    return Derivation(
        name=attrs["name"],
        system=attrs["system"],
        builder=attrs["builder"],
        args=attrs["args"],
        env=env,
        outputs=outputs,
    )


@pytest.mark.parametrize("output", ["out", "dev"])
def test_placeholder_is_builtins_placeholder(output: str) -> None:
    assert placeholder(output) == "/" + FIXTURE["placeholders"][output]


def test_a_floating_derivation_lands_where_nix_puts_it() -> None:
    leaf = FIXTURE["leaf"]
    assert drv_path(from_attrs(leaf["attrs"], out=Floating())) == leaf["drvPath"]


def test_a_fixed_derivation_lands_where_nix_puts_it() -> None:
    fixed = FIXTURE["fixed"]
    attrs = fixed["attrs"]
    output = Fixed(hash=_sri(attrs["outputHash"]), method="flat")
    assert drv_path(from_attrs(attrs, out=output)) == fixed["drvPath"]


def test_a_downstream_placeholder_is_what_nix_writes_for_a_floating_output() -> None:
    leaf = FIXTURE["leaf"]
    assert downstream_placeholder(leaf["drvPath"]) == leaf["downstream"]


def _sri(hex_digest: str) -> str:
    return "sha256-" + base64.b64encode(bytes.fromhex(hex_digest)).decode()
