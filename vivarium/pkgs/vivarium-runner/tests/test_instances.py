"""What a template instance is given: index, MACs, addresses, seed. nixidae/nixidae#1408."""

from __future__ import annotations

import re

import pytest
from vivarium_runner import Interface, MachineSpec
from vivarium_runner.instances import InstanceError, free_index, instance_spec, seed, shifted
from vivarium_runner.testing import machine_spec


def _template() -> MachineSpec:
    nic = Interface(name="eth1", nic=1, segment="fleet", addresses=(), mac="52:54:00:12:01:03", mtu=65000)
    return machine_spec("worker", index=3, interfaces=(nic,))


def test_the_lowest_free_index_from_the_first_comes_back() -> None:
    assert free_index({0, 1, 4, 6}, first=4) == 5
    assert free_index(set(), first=4) == 4


def test_no_free_index_is_an_error() -> None:
    with pytest.raises(InstanceError, match="every index from 250 to 255 is taken"):
        free_index(set(range(250, 256)), first=250)


def test_a_slot_shifts_each_address_and_keeps_its_prefix() -> None:
    assert shifted(["10.9.0.100/24", "fd00:9::100/64"], 2) == ("10.9.0.102/24", "fd00:9::102/64")


def test_a_slot_past_the_network_is_an_error() -> None:
    with pytest.raises(InstanceError, match=re.escape("outside its network 10.9.0.0/24")):
        shifted(["10.9.0.250/24"], 10)


def test_an_instance_carries_its_index_in_each_mac_and_has_no_forwards() -> None:
    one = instance_spec(_template(), "w1", 7, {"fleet": ["10.9.0.100/24"]}, slot=1)
    assert (one.name, one.index, one.inbound) == ("w1", 7, ())
    assert [(nic.mac, nic.addresses) for nic in one.interfaces] == [("52:54:00:12:01:07", ("10.9.0.101/24",))]


def test_an_instance_on_the_lan_takes_its_first_address_there() -> None:
    nic = Interface(name="vec1", nic=1, segment="k8s", addresses=(), mac="52:54:00:12:01:03", mtu=65000)
    template = machine_spec("worker", index=3, interfaces=(nic,), address=None)
    one = instance_spec(template, "w1", 7, {"k8s": ["10.100.0.50/24"]}, slot=2)
    assert (one.address, one.ip) == ("10.100.0.52/24", "10.100.0.52")
    assert instance_spec(_template(), "w1", 7, {"fleet": ["10.9.0.100/24"]}, slot=0).address is None


def test_the_seed_names_each_interface_and_the_user_data() -> None:
    one = instance_spec(_template(), "w1", 7, {}, slot=0)
    assert seed(one, "worker", "x") == {
        "name": "w1",
        "template": "worker",
        "index": 7,
        "interfaces": {"eth1": []},
        "user_data": "x",
    }
