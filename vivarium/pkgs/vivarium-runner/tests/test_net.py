"""Which segments the runner sits in, and what a hub carries."""

from __future__ import annotations

import anyio
from vivarium_runner.net import Lan, build_lans


def test_two_members_are_one_socketpair() -> None:
    lan = Lan("core", ["a/vec1", "b/vec1"])
    try:
        assert lan._ports == []
    finally:
        lan.detach()


def test_a_hub_carries_two_members_through_the_runner() -> None:
    async def main() -> bytes:
        lan = Lan("core", ["a/vec1", "b/vec1"], hub=True)
        assert len(lan._ports) == 2
        lan.start()
        try:
            a, b = lan._guest_ends["a/vec1"], lan._guest_ends["b/vec1"]
            a.send(b"frame")
            b.setblocking(False)
            with anyio.fail_after(5):
                await anyio.wait_readable(b)
            return b.recv(64)
        finally:
            lan.close()

    assert anyio.run(main) == b"frame"


def test_build_lans_makes_a_hub_of_the_segments_named() -> None:
    lans = build_lans({"core": ["a/vec1", "b/vec1"], "edge": ["a/vec2", "c/vec1"]}, hubs={"core"})
    try:
        assert {lan.name: len(lan._ports) for lan in lans} == {"core": 2, "edge": 0}
    finally:
        for lan in lans:
            lan.detach()
            for port in lan._ports:
                port.close()
