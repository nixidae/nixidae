from __future__ import annotations

from test_bundle import CAIRO, FONTCONFIG, FREETYPE, LIBPNG, font_bundle

from pynix_review.build import Outcome, Report, report
from pynix_review.bundle import to_dot
from pynix_review.plan import plan


def outcomes() -> list[Outcome]:
    return [
        Outcome(LIBPNG, "built", True, 100, 140, None),
        Outcome(FREETYPE, "substituted", True, 0, 0, None),
        Outcome(FONTCONFIG, "permanent-failure", False, 150, 160, "builder for 'fontconfig' failed with exit code 1"),
        Outcome(CAIRO, "dependency-failed", False, 0, 0, "1 dependency failed"),
    ]


def test_report_times_only_what_a_builder_ran() -> None:
    made = report(plan(font_bundle()), font_bundle(), outcomes(), 61.25)
    by_drv = {r.drv: r for r in made.results}
    assert (by_drv[LIBPNG].seconds, by_drv[LIBPNG].estimate) == (40, 38)
    assert by_drv[FREETYPE].seconds is None
    assert by_drv[FONTCONFIG].seconds is None
    assert by_drv[FONTCONFIG].estimate is None
    assert made.built_seconds == 40


def test_a_build_with_no_reported_times_has_no_seconds() -> None:
    zero = [Outcome(LIBPNG, "built", True, 0, 0, None)]
    assert report(plan(font_bundle(), budget=40), font_bundle(), zero, 1.0).results[0].seconds is None


def test_report_counts_failures_and_keeps_the_message() -> None:
    made = report(plan(font_bundle()), font_bundle(), outcomes(), 61.25)
    assert made.failed == 2
    assert [r.pname for r in made.results if not r.ok] == ["fontconfig", "cairo"]
    assert made.results[2].message == "builder for 'fontconfig' failed with exit code 1"


def test_built_dot_fills_by_outcome() -> None:
    made = report(plan(font_bundle(), budget=100), font_bundle(), outcomes()[:2], 1.0)
    dot = to_dot(font_bundle(), {r.drv: r for r in made.results})
    assert dot.startswith("digraph built {")
    line = {d: next(ln for ln in dot.splitlines() if ln.startswith(f'  "{d}" [')) for d in (LIBPNG, FREETYPE, CAIRO)}
    assert 'fillcolor="palegreen"' in line[LIBPNG]
    assert "took 40 s" in line[LIBPNG]
    assert 'fillcolor="lightgrey"' in line[FREETYPE]
    assert 'style="dotted"' in line[CAIRO]


def test_failures_fill_by_kind() -> None:
    made = report(plan(font_bundle()), font_bundle(), outcomes(), 1.0)
    dot = to_dot(font_bundle(), {r.drv: r for r in made.results})
    assert 'fillcolor="salmon"' in next(ln for ln in dot.splitlines() if ln.startswith(f'  "{FONTCONFIG}" ['))
    assert 'fillcolor="khaki"' in next(ln for ln in dot.splitlines() if ln.startswith(f'  "{CAIRO}" ['))


def test_report_round_trips() -> None:
    made = report(plan(font_bundle()), font_bundle(), outcomes(), 61.25)
    assert Report.model_validate_json(made.model_dump_json()) == made
