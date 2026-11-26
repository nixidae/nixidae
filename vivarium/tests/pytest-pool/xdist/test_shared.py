"""Two xdist workers each take a clean guest of one template.

Each run numbers its instances from i-0001, so two workers with a run of
their own get the same name. `conftest.py` checks, on the controller,
that the names differ: the workers share one pool run.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from vivarium.remote import RemoteMachine


def record(config: pytest.Config, worker_id: str, name: str) -> None:
    got = Path(config.getoption("vivarium_out")) / "got"
    got.mkdir(parents=True, exist_ok=True)
    (got / worker_id).write_text(name)


@pytest.mark.xdist_group("a")
@pytest.mark.vivarium("boxes", template="box", scope="function")
async def test_a(vm: RemoteMachine, pytestconfig: pytest.Config, worker_id: str) -> None:
    record(pytestconfig, worker_id, vm.name)


@pytest.mark.xdist_group("b")
@pytest.mark.vivarium("boxes", template="box", scope="function")
async def test_b(vm: RemoteMachine, pytestconfig: pytest.Config, worker_id: str) -> None:
    record(pytestconfig, worker_id, vm.name)
