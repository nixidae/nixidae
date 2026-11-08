"""A guest's `vivarium.control`: the dialects come from the `vivarium.controls`
entry points, and a control the guest cannot be driven with is refused
before it boots, with the guest's name.

This runs against the installed package, so it reads the entry points
pyproject.toml declares.
"""

from __future__ import annotations

import pytest
from vivarium_runner import control, routeros, sonic
from vivarium_runner.control import ControlError, ControlSpec


def test_the_dialects_are_installed() -> None:
    assert control.dialects() == {"routeros": routeros.DIALECT, "sonic": sonic.DIALECT}


@pytest.mark.parametrize(
    "spec",
    [
        ControlSpec(),
        ControlSpec("none", {"ready": "MikroTik"}),
        ControlSpec("routeros", {"configure": True}),
        ControlSpec("sonic"),
    ],
)
def test_a_good_control_passes(spec: ControlSpec) -> None:
    control.validate("one", spec)


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (ControlSpec("vyos"), "one: vivarium.control.kind is 'vyos'; have agent, none, routeros, sonic"),
        (ControlSpec("agent", {"ready": "x"}), "one: vivarium.control.config has fields the agent control"),
        (ControlSpec("none"), "one: vivarium.control.config lacks fields the none control needs: ready"),
        (ControlSpec("sonic", {"configur": True}), "fields the sonic control does not know: configur"),
    ],
)
def test_a_bad_control_names_the_guest(spec: ControlSpec, message: str) -> None:
    with pytest.raises(ControlError, match=message):
        control.validate("one", spec)


def test_none_reads_its_ready_pattern() -> None:
    ready = control.NoneConfig.of("one", ControlSpec("none", {"ready": "MikroTik [0-9.]+ "})).ready
    assert ready.search("MikroTik 7.24.5 (stable)")
