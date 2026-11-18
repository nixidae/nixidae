"""The agent finds its virtio-serial port by the name the host gave it."""

from __future__ import annotations

from pathlib import Path

import pytest
from vivarium_agent.agent import virtio_port


def test_a_virtio_port_is_found_by_name(tmp_path: Path) -> None:
    for port, name in (("vport1p1", "other"), ("vport2p1", "vivarium-agent")):
        (tmp_path / port).mkdir()
        (tmp_path / port / "name").write_text(f"{name}\n")
    assert virtio_port("vivarium-agent", tmp_path) == Path("/dev/vport2p1")
    with pytest.raises(FileNotFoundError, match="no virtio-serial port is named 'absent'"):
        virtio_port("absent", tmp_path)
