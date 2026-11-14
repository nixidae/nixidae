from __future__ import annotations

import json
from typing import Final

import anyio
from shapes import base
from test_bundle import CAIRO, FONTCONFIG, FREETYPE, LIBPNG, font_bundle, node

from pynix_review.build import (
    DOT_FILE,
    REPORT_FILE,
    SCHEMA_FILE,
    URL_FILE,
    Outcome,
    Report,
    build_message,
    failure_status,
    held_back,
    report,
    schedule,
    tail,
    write,
)
from pynix_review.bundle import DEPENDENCY_FAILED, OUT_OF_BUDGET, UNCHANGED_INPUT_FAILED, Bundle, to_dot
from pynix_review.plan import order


def outcomes() -> list[Outcome]:
    return [
        Outcome(LIBPNG, "built", True, 100, 140, None),
        Outcome(FREETYPE, "substituted", True, 0, 0, None),
        Outcome(FONTCONFIG, "permanent-failure", False, 150, 160, "builder for 'fontconfig' failed with exit code 1"),
        Outcome(CAIRO, DEPENDENCY_FAILED, False, 0, 0, "a changed input failed"),
    ]


def made(found: list[Outcome] | None = None) -> Report:
    return report(font_bundle(), outcomes() if found is None else found, budget=3600, wall_seconds=61.25)


def test_report_times_what_a_builder_ran() -> None:
    by_drv = {r.drv: r for r in made().results}
    assert (by_drv[LIBPNG].seconds, by_drv[LIBPNG].estimate) == (40, 38)
    assert by_drv[FREETYPE].seconds is None
    assert by_drv[FONTCONFIG].seconds == 10
    assert by_drv[CAIRO].seconds is None


def test_report_carries_the_log_tail_and_the_outputs() -> None:
    found = [
        Outcome(LIBPNG, "built", True, 1, 2, None, outputs={"out": "/nix/store/x-libpng", "dev": "/nix/store/y-dev"}),
        Outcome(FONTCONFIG, "permanent-failure", False, 1, 2, "error: x", log_tail="make: *** Error 1"),
    ]
    by_drv = {r.drv: r for r in made(found).results}
    assert by_drv[LIBPNG].outputs == {"out": "/nix/store/x-libpng", "dev": "/nix/store/y-dev"}
    assert (by_drv[LIBPNG].log_tail, by_drv[FONTCONFIG].log_tail) == (None, "make: *** Error 1")


def test_tail_keeps_the_last_lines_without_colour() -> None:
    log = "".join(f"line {n}\n" for n in range(100)) + "\x1b[31;1mboom\x1b[0m\n"
    assert tail(log, 3) == "line 98\nline 99\nboom"


ERROR: Final = (
    "error: Cannot build '/nix/store/3qw4-hello-2.12.3.drv'.\n"
    "       Reason: builder failed with exit code 3.\n"
    "       Output paths:\n         /nix/store/fgrl-hello-2.12.3\n"
    "       Last 25 log lines:\n       > make[1]: Leaving directory '/build/hello-2.12.3'\n"
    "       > pynix-review-probe-failure"
)


def test_a_logged_failure_message_leaves_the_quoted_log_lines_out() -> None:
    assert build_message(ERROR, logged=True) == (
        "error: Cannot build '/nix/store/3qw4-hello-2.12.3.drv'. Reason: builder failed with exit code 3. "
        "Output paths: /nix/store/fgrl-hello-2.12.3"
    )
    assert "probe-failure" in build_message(ERROR, logged=False)


def test_a_build_with_no_reported_times_has_no_seconds() -> None:
    zero = made([Outcome(LIBPNG, "built", True, 0, 0, None)])
    assert zero.results[0].seconds is None


def test_report_counts() -> None:
    counted = made([*outcomes()[:3], Outcome(CAIRO, OUT_OF_BUDGET, False, 0, 0, "not started within 60 s")])
    assert (counted.built, counted.failed, counted.out_of_budget) == (1, 1, 1)
    assert (counted.built_seconds, counted.built_estimate) == (40, 38)
    assert counted.estimate == 55 + 38 + 7 + 60


def test_nix_dependency_failed_means_an_unchanged_input_failed() -> None:
    assert failure_status(DEPENDENCY_FAILED) == UNCHANGED_INPUT_FAILED
    assert failure_status("permanent-failure") == "permanent-failure"
    counted = made([*outcomes()[:3], Outcome(CAIRO, UNCHANGED_INPUT_FAILED, False, 0, 0, "error: Cannot build")])
    assert (counted.failed, counted.unchanged_input_failed) == (2, 1)
    assert "failed 2 (1 on an unchanged input)" in to_dot(font_bundle(), counted)


def test_held_back_by_a_base_failure_is_a_base_failure_too() -> None:
    base = Outcome(FONTCONFIG, UNCHANGED_INPUT_FAILED, False, 0, 0, None)
    pr = Outcome(LIBPNG, "permanent-failure", False, 1, 2, None)
    held = held_back(CAIRO, [base])
    assert held is not None
    assert held.status == UNCHANGED_INPUT_FAILED
    mixed = held_back(CAIRO, [base, pr])
    assert mixed is not None
    assert mixed.status == DEPENDENCY_FAILED


def test_report_round_trips() -> None:
    assert Report.model_validate_json(made().model_dump_json()) == made()


async def test_write_puts_the_schema_beside_the_report(tmp_path: anyio.Path) -> None:
    out = anyio.Path(tmp_path) / "out"
    written = await write(made(), font_bundle(), out, "https://example.invalid/view")
    assert sorted(p.name for p in written) == sorted([REPORT_FILE, SCHEMA_FILE, DOT_FILE, URL_FILE])
    schema = json.loads(await (out / SCHEMA_FILE).read_text())
    assert schema["title"] == "Report"
    assert {"log_tail", "outputs"} <= set(schema["$defs"]["Built"]["properties"])
    assert await (out / URL_FILE).read_text() == "https://example.invalid/view\n"


def test_held_back_by_a_failed_input_before_one_out_of_budget() -> None:
    failed = Outcome(LIBPNG, "permanent-failure", False, 1, 2, None)
    late = Outcome(FONTCONFIG, OUT_OF_BUDGET, False, 0, 0, None)
    ok = Outcome(FREETYPE, "built", True, 1, 2, None)
    assert held_back(CAIRO, [ok]) is None
    assert held_back(CAIRO, [late, failed]) == Outcome(CAIRO, DEPENDENCY_FAILED, False, 0, 0, "a changed input failed")
    held = held_back(CAIRO, [late, ok])
    assert held is not None
    assert held.status == OUT_OF_BUDGET


async def test_schedule_builds_inputs_first_and_holds_back_after_a_failure() -> None:
    called: list[str] = []

    async def build_one(drv: str) -> Outcome:
        called.append(drv)
        if drv == FONTCONFIG:
            return Outcome(drv, "permanent-failure", False, 1, 2, "failed")
        return Outcome(drv, "built", True, 1, 2, None)

    found = await schedule(order(font_bundle()), build_one, wall=60, jobs=4)
    assert called == [LIBPNG, FREETYPE, FONTCONFIG]
    assert [o.status for o in found] == ["built", "built", "permanent-failure", DEPENDENCY_FAILED]


async def test_schedule_starts_nothing_past_the_deadline_and_lets_a_running_build_finish() -> None:
    async def build_one(drv: str) -> Outcome:
        await anyio.sleep(0.2)
        return Outcome(drv, "built", True, 1, 2, None)

    found = await schedule(order(font_bundle()), build_one, wall=0.1, jobs=4)
    assert [o.status for o in found] == ["built", OUT_OF_BUDGET, OUT_OF_BUDGET, OUT_OF_BUDGET]
    assert found[1].message == "not started within 0.1 s"
    assert found[3].message == "a changed input ran out of budget"


def wide(n: int) -> Bundle:
    source = font_bundle()
    [libpng] = [d for d in source.derivations if d.drv == LIBPNG]
    return source.model_copy(
        update={"derivations": [libpng.model_copy(update={"drv": base(f"lib{i}-1", "b")}) for i in range(n)]}
    )


async def test_schedule_runs_at_most_jobs_at_once() -> None:
    running = peak = 0

    async def build_one(drv: str) -> Outcome:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await anyio.sleep(0.01)
        running -= 1
        return Outcome(drv, "built", True, 1, 2, None)

    found = await schedule(order(wide(10)), build_one, wall=60, jobs=3)
    assert len(found) == 10
    assert peak == 3


def test_built_dot_fills_by_outcome_and_shows_both_times() -> None:
    dot = to_dot(
        font_bundle(), made([*outcomes()[:3], Outcome(CAIRO, OUT_OF_BUDGET, False, 0, 0, "not started within 60 s")])
    )
    assert dot.startswith("digraph built {")
    summary = (
        "built 1 of 4: took 40 s, est 38 s | failed 1 (0 on an unchanged input) | out of budget 1 | wall 61 s of 3600 s"
    )
    assert f'label="{summary}"' in dot
    assert 'fillcolor="palegreen"' in node(dot, LIBPNG)
    assert "est ≈ 38 s\\ntook 40 s" in node(dot, LIBPNG)
    assert 'fillcolor="lightgrey"' in node(dot, FREETYPE)
    assert 'fillcolor="salmon"' in node(dot, FONTCONFIG)
    assert "permanent-failure after 10 s" in node(dot, FONTCONFIG)
    assert 'fillcolor="gold"' in node(dot, CAIRO)
    assert "out of budget" in node(dot, CAIRO)


def test_a_dependency_failure_is_red_and_a_missing_result_dotted() -> None:
    dot = to_dot(font_bundle(), made(outcomes()[:2] + outcomes()[3:]))
    assert 'fillcolor="salmon"' in node(dot, CAIRO)
    assert 'style="dotted"' in node(dot, FONTCONFIG)
