"""A cut drops a member's frames with its carrier up; a capture is a pcap of bare Ethernet."""

import ipaddress
import struct
from pathlib import Path

from vivarium_runner import Machines, SegmentError

PCAP_HEADER = 24
RECORD_HEADER = 16
IPV4 = 0x0800


def ipv4_sources(pcap: Path) -> set[str]:
    """The source address of each IPv4 frame in *pcap*."""
    data = pcap.read_bytes()
    sources: set[str] = set()
    offset = PCAP_HEADER
    while offset + RECORD_HEADER <= len(data):
        _, _, length, _ = struct.unpack_from("<IIII", data, offset)
        frame = data[offset + RECORD_HEADER : offset + RECORD_HEADER + length]
        offset += RECORD_HEADER + length
        if len(frame) >= 34 and struct.unpack_from("!H", frame, 12)[0] == IPV4:
            sources.add(str(ipaddress.IPv4Address(frame[26:30])))
    return sources


async def test(vms: Machines) -> None:
    a, b = vms.a, vms.b

    await a.wait_until_succeeds("ping -c 1 -W 2 10.82.2.2", 60)
    pcap = vms.segment("pair").capturing
    if pcap is None:
        raise AssertionError("pair is not capturing, though `segments.pair.capture` is set")
    sources = ipv4_sources(pcap)
    if not {"10.82.2.1", "10.82.2.2"} <= sources:
        raise AssertionError(f"{pcap} holds IPv4 frames from {sorted(sources)}, want 10.82.2.1 and 10.82.2.2")
    print(f"[test] {pcap.name} holds the ping, as bare Ethernet frames")

    await a.wait_until_succeeds("ping -c 1 -W 2 10.82.1.2", 60)
    tri = vms.segment("tri")
    await tri.cut("a")
    await a.wait_until_fails("ping -c 1 -W 1 10.82.1.2", 30)
    await b.succeed("ping -c 1 -W 2 10.82.1.3")
    carrier = (await a.succeed("cat /sys/class/net/tri/carrier")).strip()
    if carrier != "1":
        raise AssertionError(f"a's tri has carrier {carrier} during the cut, want 1")
    print("[test] a is cut from tri with its carrier up, and b still reaches c")

    await tri.restore("a")
    await a.wait_until_succeeds("ping -c 1 -W 2 10.82.1.2", 60)
    print("[test] a reaches b again after the restore")

    try:
        await vms.segment("direct").cut()
    except SegmentError as error:
        print(f"[test] direct refuses a cut: {error}")
    else:
        raise AssertionError("a cut of the direct pair did not raise")
