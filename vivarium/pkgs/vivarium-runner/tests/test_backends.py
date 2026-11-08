"""The backends come from the `vivarium.backends` entry point group.

This runs against the installed package, so it reads the entry points
pyproject.toml declares, not a list in the code.
"""

from __future__ import annotations

import pytest
from vivarium_runner import backend
from vivarium_runner.backend import Container, Krun, Qemu, Uml


def test_the_four_are_installed() -> None:
    assert {name: type(one) for name, one in backend.available().items()} == {
        "uml": Uml,
        "qemu": Qemu,
        "krun": Krun,
        "container": Container,
    }


def test_get_names_what_is_installed() -> None:
    assert isinstance(backend.get("uml"), Uml)
    with pytest.raises(ValueError, match="no backend 'vm'; have container, krun, qemu, uml"):
        backend.get("vm")
