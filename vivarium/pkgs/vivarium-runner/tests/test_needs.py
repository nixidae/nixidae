"""`vivarium_runner.needs`: what a host lacks, against a root of files."""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
from vivarium_runner.needs import Missing, Need, NeedKind, missing, preflight, unmet
from vivarium_runner.spec import BackendName
from vivarium_runner.testing import machine_spec

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from vivarium_runner import MachineSpec

KVM = Need("kvm", NeedKind.DEVICE, "/dev/kvm", "QEMU runs on KVM", "load kvm")
BRIDGE = Need("br-netfilter", NeedKind.PATH, "/proc/sys/net/bridge/bridge-nf-call-iptables", "pods", "modprobe")


def test_a_path_is_met_when_it_exists(tmp_path: Path) -> None:
    assert unmet(BRIDGE, tmp_path) == f"{BRIDGE.path} does not exist"
    (tmp_path / "proc/sys/net/bridge").mkdir(parents=True)
    (tmp_path / "proc/sys/net/bridge/bridge-nf-call-iptables").write_text("1\n")
    assert unmet(BRIDGE, tmp_path) is None


def test_a_device_must_be_readable_and_writable(tmp_path: Path) -> None:
    assert unmet(KVM, tmp_path) == "/dev/kvm does not exist"
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev/kvm").touch(mode=0o400)
    assert "not readable and writable" in (unmet(KVM, tmp_path) or "")
    (tmp_path / "dev/kvm").chmod(0o600)
    assert unmet(KVM, tmp_path) is None


def test_one_item_for_each_need_names_every_guest_that_has_it(tmp_path: Path) -> None:
    guests = [machine_spec("a", needs=(KVM,)), machine_spec("b", needs=(KVM, BRIDGE)), machine_spec("c")]
    found = missing(guests, tmp_path)
    assert [item.what.split()[0] for item in found] == ["kvm", "br-netfilter"]
    assert found[0].why == "QEMU runs on KVM (guests: a, b)"
    assert found[0].remedy == "load kvm"


def test_preflight_asks_each_backend_once_for_its_own_guests() -> None:
    asked: list[tuple[str, list[str]]] = []

    class Backend:
        def __init__(self, name: str) -> None:
            self.name = name

        async def missing(self, specs: Sequence[MachineSpec]) -> list[Missing]:
            asked.append((self.name, [spec.name for spec in specs]))
            return [Missing(self.name, "why", "fix")] if self.name == "container" else []

    guests = [
        machine_spec("a", BackendName.CONTAINER),
        machine_spec("b"),
        machine_spec("c", BackendName.CONTAINER),
    ]
    found = anyio.run(preflight, guests, Backend)
    assert asked == [("container", ["a", "c"]), ("uml", ["b"])]
    assert [item.what for item in found] == ["container"]
