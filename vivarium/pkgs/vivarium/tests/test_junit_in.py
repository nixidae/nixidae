from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from vivarium.events import Event, Kind
from vivarium.junit_in import DIRECTORY, Case, parse
from vivarium.session import Session
from vivarium.sinks import Broadcast
from vivarium.spec import Spec

if TYPE_CHECKING:
    from pathlib import Path

# The shape pytest's --junitxml writes, trimmed.
PYTEST = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="4">
  <testcase classname="tests.unit.test_a" name="test_ok" time="0.010"/>
  <testcase classname="tests.unit.test_a" name="test_bad" time="0.200">
    <failure message="assert 1 == 2">def test_bad():
&gt;       assert 1 == 2</failure>
  </testcase>
  <testcase classname="tests.unit.test_a" name="test_broken" time="0.001">
    <error message="fixture failed">boom</error>
  </testcase>
  <testcase classname="tests.unit.test_a" name="test_later" time="0">
    <skipped type="pytest.skip" message="not today"/>
  </testcase>
</testsuite></testsuites>"""


class TestParse:
    def test_every_outcome(self) -> None:
        cases = parse(PYTEST)
        assert [(c.name.split("::")[-1], c.outcome) for c in cases] == [
            ("test_ok", "passed"),
            ("test_bad", "failed"),
            ("test_broken", "error"),
            ("test_later", "skipped"),
        ]

    def test_the_name_carries_the_class(self) -> None:
        assert parse(PYTEST)[0].name == "tests.unit.test_a::test_ok"

    def test_a_failure_keeps_message_and_body(self) -> None:
        bad = parse(PYTEST)[1]
        assert bad == Case(
            "tests.unit.test_a::test_bad",
            "failed",
            0.2,
            message="assert 1 == 2",
            error="def test_bad():\n>       assert 1 == 2",
        )

    def test_a_skip_keeps_its_reason(self) -> None:
        assert parse(PYTEST)[3].reason == "not today"

    def test_a_bare_testsuite_root(self) -> None:
        """go-junit-report writes <testsuites>; some runners write one
        <testsuite> alone."""
        text = '<testsuite><testcase name="TestX" time="1.5"/></testsuite>'
        assert parse(text) == [Case("TestX", "passed", 1.5)]

    def test_not_xml_says_so(self) -> None:
        with pytest.raises(ValueError, match="not JUnit XML"):
            parse("<testsuite><testcase")


class Collect:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def close(self) -> None:
        pass


def write_suite(out: Path, name: str, mtime_ns: int) -> None:
    directory = out / "artifacts" / "one" / DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.xml"
    path.write_text(f'<testsuite><testcase name="{name}" time="0"/></testsuite>')
    os.utime(path, ns=(mtime_ns, mtime_ns))


class TestASecondRunIntoTheSameOut:
    def test_reads_only_its_own_files(self, tmp_path: Path) -> None:
        """Measured 2026-11-07 (nixidae/nixidae#1201): a run into an
        `--out` two days old reported 2025 of the old run's cases, one of
        them a failure of a test deleted since."""
        out = tmp_path / "out"
        write_suite(out, "old", 1_000)
        write_suite(out, "rewritten", 1_000)
        sink = Collect()
        session = Session(Spec(machines=[], phases=[]), out, sink=Broadcast([sink]))
        write_suite(out, "new", 2_000)
        write_suite(out, "rewritten", 2_000)
        session._cases_from_guests("boot")
        assert sorted(e.text for e in sink.events if e.kind is Kind.CASE) == ["new", "rewritten"]
