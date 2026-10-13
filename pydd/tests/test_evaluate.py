"""The context ``to_value`` attaches, read back by Nix."""

from __future__ import annotations

from typing import Any

import huggorm
import pytest

from pydd.modes.evaluate import to_value

BASH = "0" * 32 + "-bash"
DRV = "1" * 32 + "-fetch.drv"
OUT = "2" * 32 + "-fetch"


@pytest.fixture
def state() -> Any:
    return huggorm.EvalState(huggorm.Store("dummy://"))


def test_a_store_path_depends_on_its_store_path(state: Any) -> None:
    value = to_value(state, f"/nix/store/{BASH}/bin/sh")
    assert value.string_context() == [BASH]


def test_a_plain_string_depends_on_nothing(state: Any) -> None:
    assert to_value(state, "/home/me/project").string_context() == []


def test_a_candidate_output_depends_on_that_output(state: Any) -> None:
    candidate = {"drv": f"/nix/store/{DRV}", "outputs": {"out": f"/nix/store/{OUT}"}}
    value = to_value(state, {"c": candidate})
    assert value.get("c").get("outputs").get("out").string_context() == [f"!out!{DRV}"]
    assert value.get("c").get("drv").string_context() == [DRV]


def test_every_json_shape_arrives(state: Any) -> None:
    data = {"list": [1, 2.5, True, None], "nested": {"s": "x"}}
    assert to_value(state, data).to_json() == '{"list":[1,2.5,true,null],"nested":{"s":"x"}}'
