"""Which segments the runner sits in, and what a hub carries, cuts and captures."""

from __future__ import annotations

import socket
import struct
from pathlib import Path

import anyio
import pytest
from vivarium_runner.net import Lan, SegmentError, build_lans


async def _receive(sock: socket.socket, wait: float = 5) -> bytes | None:
    """The next frame on *sock*, or None if none comes within *wait*."""
    sock.setblocking(False)
    with anyio.move_on_after(wait):
        await anyio.wait_readable(sock)
        return sock.recv(64)
    return None


def test_two_members_are_one_socketpair() -> None:
    lan = Lan("core", ["a/vec1", "b/vec1"])
    try:
        assert lan._ports == []
        assert not lan.is_hub
    finally:
        lan.detach()


def test_a_hub_carries_two_members_through_the_runner() -> None:
    async def main() -> bytes | None:
        lan = Lan("core", ["a/vec1", "b/vec1"], hub=True)
        assert len(lan._ports) == 2
        lan.start()
        try:
            lan._guest_ends["a/vec1"].send(b"frame")
            return await _receive(lan._guest_ends["b/vec1"])
        finally:
            lan.close()

    assert anyio.run(main) == b"frame"


def test_build_lans_makes_a_hub_of_the_segments_named(tmp_path: Path) -> None:
    lans = build_lans({"core": ["a/vec1", "b/vec1"], "edge": ["a/vec2", "c/vec1"]}, hubs={"core"}, pcap_dir=tmp_path)
    try:
        assert {lan.name: len(lan._ports) for lan in lans} == {"core": 2, "edge": 0}
        assert [lan.pcap for lan in lans] == [tmp_path / "core.pcap", tmp_path / "edge.pcap"]
    finally:
        for lan in lans:
            lan.detach()
            for port in lan._ports:
                port.sock.close()


def test_a_cut_member_neither_sends_nor_hears_and_the_rest_carry_on() -> None:
    said: list[str] = []

    async def main() -> None:
        lan = Lan("core", ["a/eth1", "b/eth1", "c/eth1"], on_change=said.append)
        lan.start()
        a, b, c = (lan._guest_ends[f"{guest}/eth1"] for guest in "abc")
        try:
            await lan.cut("a")
            assert lan.cut_members == ["a/eth1"]
            a.send(b"from a")
            b.send(b"from b")
            assert await _receive(c) == b"from b"
            assert await _receive(a, wait=0.2) is None
            assert await _receive(c, wait=0.2) is None

            await lan.restore("a/eth1")
            assert lan.cut_members == []
            b.send(b"again")
            assert await _receive(a) == b"again"
            assert lan.dropped == 0
        finally:
            lan.close()

    anyio.run(main)
    assert said == ["segment core: cut a/eth1", "segment core: restored a/eth1"]


def test_a_cut_of_the_whole_segment_carries_nothing() -> None:
    async def main() -> None:
        lan = Lan("core", ["a/eth1", "b/eth1"], hub=True)
        lan.start()
        try:
            await lan.cut()
            lan._guest_ends["a/eth1"].send(b"lost")
            assert await _receive(lan._guest_ends["b/eth1"], wait=0.2) is None
            await lan.restore()
            lan._guest_ends["a/eth1"].send(b"kept")
            # The frame sent during the cut is gone, not queued.
            assert await _receive(lan._guest_ends["b/eth1"]) == b"kept"
        finally:
            lan.close()

    anyio.run(main)


def test_a_direct_pair_refuses_a_cut_and_names_the_option() -> None:
    async def main() -> None:
        lan = Lan("core", ["a/eth1", "b/eth1"])
        try:
            with pytest.raises(SegmentError, match=r"segments\.core\.hub = true"):
                await lan.cut()
            with pytest.raises(SegmentError, match="cannot capture"):
                await lan.capture()
        finally:
            lan.close()

    anyio.run(main)


def test_an_unknown_member_is_named() -> None:
    async def main() -> None:
        lan = Lan("core", ["a/eth1", "b/eth1"], hub=True)
        try:
            with pytest.raises(SegmentError, match=r"no member 'z'; it has a/eth1, b/eth1"):
                await lan.cut("z")
        finally:
            lan.close()

    anyio.run(main)


def test_a_capture_is_a_pcap_of_every_frame_sent(tmp_path: Path) -> None:
    pcap = tmp_path / "pcap" / "core.pcap"

    async def main() -> None:
        lan = Lan("core", ["a/eth1", "b/eth1"], hub=True, pcap=pcap)
        lan.start()
        try:
            assert await lan.capture() == pcap
            lan._guest_ends["a/eth1"].send(b"one")
            assert await _receive(lan._guest_ends["b/eth1"]) == b"one"
            await lan.cut("b")
            lan._guest_ends["b/eth1"].send(b"unheard")
            for _ in range(50):
                if pcap.stat().st_size > 24 + 16 + 3:
                    break
                await anyio.sleep(0.01)
            assert lan.capturing == pcap
        finally:
            lan.close()
        assert lan.capturing is None

    anyio.run(main)
    data = pcap.read_bytes()
    magic, major, minor, _, _, snaplen, linktype = struct.unpack_from("<IHHiIII", data)
    assert (magic, major, minor, snaplen, linktype) == (0xA1B2C3D4, 2, 4, 65536, 1)
    frames = []
    offset = 24
    while offset < len(data):
        _, _, included, original = struct.unpack_from("<IIII", data, offset)
        assert included == original
        frames.append(data[offset + 16 : offset + 16 + included])
        offset += 16 + included
    assert frames == [b"one", b"unheard"]
