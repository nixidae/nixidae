"""Plain pytest against vivarium guests (nixidae/nixidae#1413).

Run `pytest` here, with `packages.vivarium-eval.env` on PATH.
`pytest --vivarium-keep` leaves the runs up, and the next pytest takes them.
"""

from __future__ import annotations

import pytest


@pytest.mark.vivarium("one", scope="module")
async def test_hostname(vm) -> None:  # noqa: ANN001 -- a RemoteMachine
    assert (await vm.succeed("hostname")).stdout.strip() == "vm"


@pytest.mark.vivarium("one", scope="module")
async def test_uplink_is_vsock(vm) -> None:  # noqa: ANN001 -- a RemoteMachine
    assert await vm.get("uplink_ip") == "10.0.2.15"
    rc = (await vm.execute("ip netns list | grep -q uplink")).returncode
    assert rc == 0


@pytest.mark.vivarium("pair")
class TestPair:
    async def test_a_reaches_b(self, vms) -> None:  # noqa: ANN001 -- RemoteMachines by name
        await vms["a"].succeed("ping -c1 -W2 192.168.99.3")

    async def test_b_reaches_a(self, vms) -> None:  # noqa: ANN001 -- RemoteMachines by name
        await vms["b"].succeed("ping -c1 -W2 192.168.99.2")
