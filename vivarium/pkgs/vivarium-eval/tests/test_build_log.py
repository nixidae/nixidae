"""What `vivarium run` prints and keeps while it builds."""

from __future__ import annotations

import io
import json
from pathlib import Path

import huggorm
from vivarium.monitor import BUILD, PLANNED, SUBSTITUTE
from vivarium_eval.build_log import FAILED, BuildRecord, Recorder, line

INFO = int(huggorm.Verbosity.INFO)


def test_a_message_is_printed_as_nix_prints_it() -> None:
    assert line(BuildRecord("msg", text="warning: dirty tree")) == "warning: dirty tree"


def test_an_activity_prints_its_dots_line_at_the_default_level() -> None:
    shown = BuildRecord("start", id=2, type=105, level=INFO, text="building 'x.drv'")
    hidden = BuildRecord("start", id=3, type=109, level=int(huggorm.Verbosity.VOMIT), text="querying y")
    assert line(shown) == "building 'x.drv'..."
    assert line(hidden) is None


def test_progress_is_not_printed() -> None:
    progress = BuildRecord("result", id=1, type=int(huggorm.ResultType.PROGRESS), fields=(0, 1, 0, 0))
    assert line(progress) is None


def test_a_log_line_stays_in_the_file(tmp_path: Path) -> None:
    stderr = io.StringIO()
    recorder = Recorder(tmp_path / "build.jsonl", stderr)
    log_line = BuildRecord("result", id=2, type=int(huggorm.ResultType.BUILD_LOG_LINE), fields=("compiling",))
    recorder.write(log_line)
    recorder.failed("error: builder for x failed")
    recorder.close()
    records = [json.loads(raw) for raw in (tmp_path / "build.jsonl").read_text().splitlines()]
    assert [record["action"] for record in records] == ["result", FAILED]
    assert records[0]["fields"] == ["compiling"]
    assert records[1]["text"] == "error: builder for x failed"
    assert stderr.getvalue() == ""


def test_the_monitor_names_nix_s_activities() -> None:
    """`vivarium` reads `build.jsonl` without huggorm, so it spells the numbers."""
    assert BUILD == int(huggorm.ActivityType.BUILD)
    assert SUBSTITUTE == int(huggorm.ActivityType.SUBSTITUTE)


def test_a_plan_is_one_record_per_kind(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "build.jsonl", io.StringIO())
    recorder.planned(["/nix/store/b.drv", "/nix/store/a.drv"], ["/nix/store/s"])
    recorder.close()
    records = [json.loads(raw) for raw in (tmp_path / "build.jsonl").read_text().splitlines()]
    assert [(r["action"], r["type"], r["fields"]) for r in records] == [
        (PLANNED, BUILD, ["/nix/store/a.drv", "/nix/store/b.drv"]),
        (PLANNED, SUBSTITUTE, ["/nix/store/s"]),
    ]
