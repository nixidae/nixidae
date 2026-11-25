"""Container guests launch at once, and the first launch probes the host for all of them."""

from __future__ import annotations

import functools

import anyio
import pytest
from vivarium_runner import container


def test_two_launches_probe_the_host_once(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[bool] = []

    async def probe(*, lan: bool, host: container.Host) -> list[container.Missing]:
        asked.append(lan)
        # A probe waits on processes: the other launch runs meanwhile.
        await anyio.lowlevel.checkpoint()
        return []

    monkeypatch.setattr(container, "probe", probe)
    host = container.Host()

    async def main() -> None:
        async with anyio.create_task_group() as tg:
            for _ in range(2):
                tg.start_soon(functools.partial(host.missing, lan=True))

    anyio.run(main)
    assert asked == [True]
