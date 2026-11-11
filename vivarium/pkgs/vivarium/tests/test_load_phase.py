"""A phase script loads as a module, as Python imports one."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from vivarium.session import SessionError, load_phase

if TYPE_CHECKING:
    from pathlib import Path

DATACLASS_PHASE = """
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Point:
    x: int


async def test(vms: object) -> None:
    pass
"""


def test_a_dataclass_under_string_annotations_loads(tmp_path: Path) -> None:
    # nixidae/nixidae#1222: without a sys.modules entry, the dataclass
    # decorator fails on the string annotation.
    script = tmp_path / "point.py"
    script.write_text(DATACLASS_PHASE)
    assert callable(load_phase(script))


def test_a_script_loads_again_after_an_edit(tmp_path: Path) -> None:
    script = tmp_path / "again.py"
    script.write_text("async def test(vms: object) -> None:\n    pass\n")
    first = load_phase(script)
    script.write_text("async def test(vms: object) -> None:\n    raise RuntimeError\n")
    assert load_phase(script) is not first


def test_a_script_without_test_is_refused(tmp_path: Path) -> None:
    script = tmp_path / "empty.py"
    script.write_text("x = 1\n")
    with pytest.raises(SessionError, match="exports no `test`"):
        load_phase(script)
