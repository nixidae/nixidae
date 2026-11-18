from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from vivarium.roots import Roots


def open_files() -> set[Path]:
    """What a collector's runtime-root walk reads for this process."""
    fds = Path("/proc/self/fd")
    found = set()
    for fd in os.listdir(fds):
        try:
            found.add(Path(os.readlink(fds / fd)))
        except OSError:
            continue
    return found


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc, as Nix's runtime-root walk does on Linux")
def test_a_held_spec_is_an_open_file_through_its_link(tmp_path: Path) -> None:
    """The run names its spec through a `result` link; the root is the file."""
    spec = tmp_path / "spec.json"
    spec.write_text("{}")
    link = tmp_path / "result"
    link.symlink_to(spec)
    roots = Roots()
    roots.hold(link)
    roots.hold(spec)
    assert roots.held == [spec]
    assert spec in open_files()
    roots.close()
    assert spec not in open_files()
