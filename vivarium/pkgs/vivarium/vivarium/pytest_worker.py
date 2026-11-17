"""The guest's end of a test that runs in a guest (`vivarium.pytest_host`).

The host unpacks its project at the same path in the guest, puts its own
Python in the guest's store view, and runs:

    python -m vivarium.pytest_worker REPORTS [--coverage FILE ...] -- PYTEST-ARGS... NODEIDS...

This is plain pytest with one more plugin. It writes each report pytest
makes, in the form xdist sends between processes
(`pytest_report_to_serializable`), one JSON object a line to REPORTS.
The host turns each back into a report and hands it to its own pytest,
so its reporters see an ordinary test. REPORTS is under `/artifacts`,
which the host reads.

With `--coverage`, coverage.py measures the run and saves to FILE. It
starts before pytest collects, so a project module is measured from its
import. The host puts FILE beside pytest-cov's data file, whose combine
merges it: the paths match, because the project is at its own path.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path
from typing import IO, TYPE_CHECKING

import coverage
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
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


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="python -m vivarium.pytest_worker")
    top.add_argument("reports", type=Path)
    top.add_argument("--coverage", type=Path, help="measure the run with coverage.py, saved here")
    top.add_argument("--cov-source", action="append", help="as pytest-cov's --cov, once each")
    top.add_argument("--cov-branch", action="store_true")
    top.add_argument("--cov-config", default=".coveragerc")
    return top


@contextlib.contextmanager
def measured(args: argparse.Namespace) -> Iterator[None]:
    if args.coverage is None:
        yield
        return
    cov = coverage.Coverage(
        data_file=str(args.coverage),
        source=args.cov_source,
        branch=args.cov_branch or None,
        config_file=args.cov_config,
    )
    cov.start()
    try:
        yield
    finally:
        cov.stop()
        cov.save()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--" not in argv:
        parser().error("pytest's arguments follow --")
    split = argv.index("--")
    args = parser().parse_args(argv[:split])
    args.reports.parent.mkdir(parents=True, exist_ok=True)
    with args.reports.open("w") as out, measured(args):
        # Not the host plugin here: these tests are already where they run.
        return int(
            pytest.main([*argv[split + 1 :], "-p", "no:vivarium", "-p", "no:cacheprovider"], plugins=[Recorder(out)])
        )


if __name__ == "__main__":
    raise SystemExit(main())
