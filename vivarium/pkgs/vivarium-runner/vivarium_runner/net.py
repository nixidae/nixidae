"""Host-side wiring for the guests' ``vec1`` interfaces.

UML's vector transport can take a pre-opened fd carrying raw Ethernet
frames, so an L2 segment is just a set of connected sockets.  Two guests
on a segment get the two ends of one ``SOCK_SEQPACKET`` socketpair and
the host stays out of the data path entirely.  Three or more need
something to fan frames out, which is what :class:`Lan` does when it has
to -- a learning-free hub that floods every frame to every other port.
One guest on its own gets a segment with nobody else on it, which is a
cable into a bucket: `vec1` exists and carries an address, and whatever
it says goes nowhere.

How much a segment carries comes down to how many frames may be in
flight in a socketpair, and AF_UNIX bounds that two ways:

  * the receiver's queue length, capped by ``net.unix.max_dgram_qlen``
    -- 10 in a fresh network namespace, which is what a Nix build
    sandbox gets, and not writable there;
  * the *sender's* ``SO_SNDBUF``, which every queued frame is charged
    against until the receiver reads it.

Only the second is ours to set, so we set it to the maximum the host
allows.  The first is why guests use a large MTU (see ``vivarium.mtu``):
ten 64K frames is a window worth having, ten 1500-byte ones is 15K, and
that difference is most of the throughput between two guests.
"""

from __future__ import annotations

import asyncio
import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

_FRAME_MAX = 65536

_PCAP_HEADER = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, _FRAME_MAX, 1)
"""pcap's global header: microsecond stamps, link type 1 (Ethernet)."""

_BURST = 64
"""Frames to take from one port before giving the others a turn."""

_WANTED_SNDBUF = 8 << 20
"""Asked for on both ends of every port.  The kernel silently clamps it
to ``net.core.wmem_max``, so asking for more than we can have costs
nothing.  There is no matching ``SO_RCVBUF``: an AF_UNIX datagram is
charged to whoever sent it until the far side reads it, so the sending
socket's buffer is the only one that bounds a segment."""


class SegmentError(ValueError):
    """A cut or a capture the segment cannot do."""


@dataclass(slots=True)
class _Port:
    member: str
    sock: socket.socket
    cut: bool = False


class _Pcap:
    """Frames to a pcap file, flushed by the caller after each burst."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file: BinaryIO = path.open("wb")
        self._file.write(_PCAP_HEADER)
        self._file.flush()

    def write(self, frame: bytes) -> None:
        now = time.time()
        seconds = int(now)
        self._file.write(struct.pack("<IIII", seconds, int((now - seconds) * 1e6), len(frame), len(frame)))
        self._file.write(frame)

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        self._file.close()


class Lan:
    """One Ethernet segment connecting the named machines.

    ``fds[name]`` is the fd to hand that machine's UML process; it must
    stay open in this process until the child has been spawned.
    """

    def __init__(
        self,
        name: str,
        members: list[str],
        hub: bool = False,
        pcap: Path | None = None,
        on_change: Callable[[str], None] | None = None,
    ) -> None:
        """*hub* passes two members' frames through the runner too, where
        it sees them; otherwise two are one socketpair. *pcap* is where
        :meth:`capture` writes; *on_change* hears each cut, restore and
        capture."""
        if not members:
            raise ValueError(f"lan {name!r} has no machines on it")
        self.name = name
        self.members = list(members)
        self.pcap = pcap
        self.on_change = on_change
        self.fds: dict[str, int] = {}
        self.dropped = 0
        self._guest_ends: dict[str, socket.socket] = {}
        self._ports: list[_Port] = []
        self._capture: _Pcap | None = None

        if len(members) == 1:
            # A guest alone on a segment: it still wants vec1 to exist and
            # carry its address -- a node in a cluster of one is a real
            # thing to test -- so it gets one end of a pair and the host
            # quietly holds the other.
            guest_end, host_end = self._pair()
            self._guest_ends = {members[0]: guest_end}
            self._ports = [_Port(members[0], host_end)]
        elif len(members) == 2 and not hub:
            # Point to point: no host involvement, no copying.
            self._guest_ends = dict(zip(members, self._pair(), strict=True))
        else:
            for member in members:
                host_end, guest_end = self._pair()
                self._ports.append(_Port(member, host_end))
                self._guest_ends[member] = guest_end
        self.fds = {member: sock.fileno() for member, sock in self._guest_ends.items()}

    @property
    def is_hub(self) -> bool:
        """The runner sees this segment's frames: it can cut and capture."""
        return bool(self._ports)

    @property
    def cut_members(self) -> list[str]:
        return [port.member for port in self._ports if port.cut]

    @property
    def capturing(self) -> Path | None:
        return self._capture.path if self._capture is not None else None

    def _hub_or_raise(self, what: str) -> None:
        if not self.is_hub:
            raise SegmentError(
                f"segment {self.name}: cannot {what}: its two members are one socketpair, "
                f"and the runner does not see its frames. Set `segments.{self.name}.hub = true`."
            )

    def _select(self, member: str | None) -> list[_Port]:
        """Every port for none, a guest's ports for `a`, one port for `a/eth1`."""
        if member is None:
            return list(self._ports)
        chosen = [port for port in self._ports if member in (port.member, port.member.split("/")[0])]
        if not chosen:
            raise SegmentError(f"segment {self.name} has no member {member!r}; it has {', '.join(self.members)}")
        return chosen

    def _say(self, text: str) -> None:
        if self.on_change is not None:
            self.on_change(f"segment {self.name}: {text}")

    async def cut(self, member: str | None = None) -> None:
        """Drop every frame to and from *member* (every member for none).

        The guest's interface keeps its carrier: a routing protocol finds
        the failure from its hold timer.
        """
        self._set_cut(member, cut=True)

    async def restore(self, member: str | None = None) -> None:
        """Carry *member*'s frames again (every member for none)."""
        self._set_cut(member, cut=False)

    def _set_cut(self, member: str | None, *, cut: bool) -> None:
        self._hub_or_raise("cut" if cut else "restore")
        ports = self._select(member)
        for port in ports:
            port.cut = cut
        self._say(f"{'cut' if cut else 'restored'} {', '.join(port.member for port in ports)}")

    async def capture(self) -> Path:
        """Write every frame a member sends to :attr:`pcap`, from now on.

        A cut member's frames too: they show what the guest sent while
        nobody heard it. A second capture starts the file again.
        """
        self._hub_or_raise("capture")
        if self.pcap is None:
            raise SegmentError(f"segment {self.name}: no pcap path to capture to")
        self._stop_capture()
        self._capture = _Pcap(self.pcap)
        self._say(f"capturing to {self.pcap}")
        return self.pcap

    async def end_capture(self) -> None:
        """Stop writing frames; the file stays."""
        self._stop_capture()

    def _stop_capture(self) -> None:
        if self._capture is not None:
            self._capture.close()
            self._capture = None
            self._say("capture stopped")

    @staticmethod
    def _pair() -> tuple[socket.socket, socket.socket]:
        pair = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        for sock in pair:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, _WANTED_SNDBUF)
        return pair

    def start(self) -> None:
        """Start flooding frames between ports, if this segment needs a hub."""
        loop = asyncio.get_running_loop()
        for port in self._ports:
            port.sock.setblocking(False)
            others = [other for other in self._ports if other is not port]
            loop.add_reader(port.sock, self._flood, port, others)

    def _flood(self, src: _Port, others: list[_Port]) -> None:
        """Copy everything readable on *src* to every other port.

        A callback rather than a task per frame: two awaits and the
        futures behind them cost more than the copy itself, and this runs
        once per frame on the segment.  Ports that will not take a frame
        have it dropped, which is what a switch does with a congested
        port -- blocking here would stall every other port as well.

        A cut port is still read: frames left queued would arrive in a
        burst at the restore, which no real link does.
        """
        capture = self._capture
        try:
            for _ in range(_BURST):
                try:
                    frame = src.sock.recv(_FRAME_MAX)
                except (BlockingIOError, InterruptedError):
                    return
                except OSError:
                    self._drop_port(src)
                    return
                if not frame:
                    self._drop_port(src)
                    return
                if capture is not None:
                    capture.write(frame)
                if src.cut:
                    continue
                for dst in others:
                    if dst.cut:
                        continue
                    try:
                        dst.sock.send(frame)
                    except OSError:
                        self.dropped += 1
        finally:
            # Once a burst, not once at close: a phase reads the file
            # live, and a killed runner keeps what it wrote.
            if capture is not None:
                capture.flush()

    def _drop_port(self, src: _Port) -> None:
        """Stop listening to a port whose guest has gone away."""
        asyncio.get_running_loop().remove_reader(src.sock)

    def detach(self, prefixes: list[str] | None = None) -> None:
        """Drop our copies of the guests' fds once those guests have spawned.

        Until this runs, a guest's end is held open here too, so no peer
        would ever see the segment go quiet when that guest dies. Guests
        boot at different times, so *prefixes* names the members whose
        ends go (`"a/"` is every link of guest `a`); none is all of them.
        """
        for member in list(self._guest_ends):
            if prefixes is None or any(member.startswith(prefix) for prefix in prefixes):
                self._guest_ends.pop(member).close()

    def close(self) -> None:
        if self.dropped:
            print(
                f"[test] lan {self.name}: dropped {self.dropped} frames on congested ports",
                flush=True,
            )
        self._stop_capture()
        loop = asyncio.get_running_loop()
        for port in self._ports:
            loop.remove_reader(port.sock)
        self.detach()
        for port in self._ports:
            port.sock.close()
        self._ports.clear()


def build_lans(
    networks: dict[str, list[str]],
    hubs: set[str] | frozenset[str] = frozenset(),
    pcap_dir: Path | None = None,
    on_change: Callable[[str], None] | None = None,
) -> list[Lan]:
    """Create a :class:`Lan` per segment in ``{segment: [machine, ...]}``.

    One in *hubs* is always a hub. Each captures to ``<pcap_dir>/<segment>.pcap``.
    """
    return [
        Lan(
            name,
            members,
            hub=name in hubs,
            pcap=pcap_dir / f"{name}.pcap" if pcap_dir is not None else None,
            on_change=on_change,
        )
        for name, members in sorted(networks.items())
    ]
