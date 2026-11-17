"""The guest's end of a test that runs in a guest (`vivarium.pytest_host`).

The host unpacks its project at the same path in the guest, puts its own
Python in the guest's store view, and runs:

    python -m vivarium.pytest_worker REPORTS -- PYTEST-ARGS... NODEIDS...

This is plain pytest with one more plugin. It writes each report pytest
makes, in the form xdist sends between processes
(`pytest_report_to_serializable`), one JSON object a line to REPORTS.
The host turns each back into a report and hands it to its own pytest,
so its reporters see an ordinary test. REPORTS is under `/artifacts`,
which the host reads.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import IO, TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from typing import Any


class Recorder:
    """Writes every test and collection report as it is made."""

    def __init__(self, out: IO[str]) -> None:
        self.out = out
        self.config: pytest.Config | None = None

    def pytest_configure(self, config: pytest.Config) -> None:
        self.config = config

    def _write(self, report: pytest.TestReport | pytest.CollectReport) -> None:
        if self.config is None:
            raise RuntimeError("a report before pytest_configure")
        data: dict[str, Any] = self.config.hook.pytest_report_to_serializable(config=self.config, report=report)
        self.out.write(json.dumps(data) + "\n")
        self.out.flush()

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self._write(report)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self._write(report)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2 or args[1] != "--":
        print("usage: python -m vivarium.pytest_worker REPORTS -- PYTEST-ARGS...", file=sys.stderr)
        return 2
    reports, rest = Path(args[0]), args[2:]
    reports.parent.mkdir(parents=True, exist_ok=True)
    with reports.open("w") as out:
        # Not the host plugin here: these tests are already where they run.
        return int(pytest.main([*rest, "-p", "no:vivarium", "-p", "no:cacheprovider"], plugins=[Recorder(out)]))


if __name__ == "__main__":
    raise SystemExit(main())
