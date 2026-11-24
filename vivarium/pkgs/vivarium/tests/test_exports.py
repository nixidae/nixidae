from __future__ import annotations

import json
from typing import TYPE_CHECKING

import anyio
import pytest
from vivarium import exports
from vivarium_runner import MachineError, Machines

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.anyio
async def test_two_phases_at_once_both_land(tmp_path: Path) -> None:
    sink = exports.Exports(tmp_path)
    first, second = Machines(), Machines()
    first.exports = second.exports = sink
    async with anyio.create_task_group() as group:
        for index in range(20):
            group.start_soon((first if index % 2 else second).export, f"V{index}", str(index))
    assert exports.read(tmp_path) == {f"V{index}": str(index) for index in range(20)}
    assert [each.name for each in tmp_path.iterdir()] == [exports.FILE]


@pytest.mark.anyio
async def test_a_later_value_replaces_one(tmp_path: Path) -> None:
    sink = exports.Exports(tmp_path)
    await sink.set("KUBECONFIG", "/a")
    await sink.set("KUBECONFIG", "/b")
    assert exports.read(tmp_path) == {"KUBECONFIG": "/b"}


@pytest.mark.anyio
async def test_a_bad_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(exports.ExportError):
        await exports.Exports(tmp_path).set("A B", "x")
    assert not (tmp_path / exports.FILE).exists()


@pytest.mark.anyio
async def test_a_run_with_no_sink_refuses() -> None:
    with pytest.raises(MachineError, match="exports nothing"):
        await Machines().export("A", "b")


def test_nothing_exported_reads_empty(tmp_path: Path) -> None:
    assert exports.read(tmp_path) == {}


@pytest.mark.parametrize("content", [["A"], {"A": 1}, {"A B": "x"}])
def test_a_malformed_file_is_refused(tmp_path: Path, content: object) -> None:
    (tmp_path / exports.FILE).write_text(json.dumps(content))
    with pytest.raises(exports.ExportError):
        exports.read(tmp_path)
