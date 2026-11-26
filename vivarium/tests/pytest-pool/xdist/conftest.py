from __future__ import annotations

from pathlib import Path

import pytest


def pytest_sessionfinish(session: pytest.Session) -> None:
    # On the controller only: xdist sets `workerinput` on a worker's config.
    if hasattr(session.config, "workerinput"):
        return
    got_dir = Path(session.config.getoption("vivarium_out")) / "got"
    got = {path.name: path.read_text() for path in got_dir.iterdir()} if got_dir.is_dir() else {}
    if len(got) != 2 or len(set(got.values())) != 2:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(f"want two workers with two guests of one pool run; got {got}", red=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
