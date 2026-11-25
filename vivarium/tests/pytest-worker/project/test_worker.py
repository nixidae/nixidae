"""The guest's pytest worker, as a user drives it (nixidae/nixidae#1413).

Each test runs pytest on a small project whose tests run in a guest, then
reads what that pytest printed and what the guest left in `/artifacts`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

IN_GUEST = """
import socket
from pathlib import Path

import pytest

pytestmark = pytest.mark.vivarium("one", guest="vm", scope="session")
"""


def test_x_stops_the_guest_and_tears_down(pytester: pytest.Pytester, tmp_path: Path) -> None:
    pytester.makepyfile(
        test_x=IN_GUEST
        + """
MARKS = Path("/artifacts/marks")


@pytest.fixture(scope="module")
def marks():
    MARKS.mkdir(exist_ok=True)
    yield MARKS
    (MARKS / "torn-down").touch()


def test_passes(marks):
    (marks / "first").touch()


def test_fails(marks):
    raise AssertionError("on purpose")


def test_never_runs(marks):
    (marks / "third").touch()
"""
    )
    out = tmp_path / "runs"
    result = pytester.runpytest_subprocess("-x", f"--vivarium-out={out}")
    result.assert_outcomes(passed=1, failed=1)
    [marks] = out.glob("*/artifacts/vm/marks")
    assert sorted(path.name for path in marks.iterdir()) == ["first", "torn-down"]


def test_shows_what_the_guest_said(pytester: pytest.Pytester, tmp_path: Path) -> None:
    pytester.makepyfile(
        test_said=IN_GUEST
        + """
def test_prints():
    print("hello from", socket.gethostname())
""",
        test_host_only=IN_GUEST
        + """
if socket.gethostname() == "vm":
    raise ImportError("this module imports on the host alone")


def test_unreached():
    pass
""",
    )
    result = pytester.runpytest_subprocess("-rP", f"--vivarium-out={tmp_path / 'runs'}")
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["*hello from vm*"])
    result.stdout.fnmatch_lines(["*did not collect it*", "*this module imports on the host alone*"])
