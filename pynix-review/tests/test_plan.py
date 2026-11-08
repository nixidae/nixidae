from __future__ import annotations

from shapes import base
from test_bundle import CAIRO, FONTCONFIG, FREETYPE, LIBPNG, font_bundle

from pynix_review.bundle import Bundle
from pynix_review.plan import Plan, plan

# Costs: libpng 38, freetype 7, fontconfig untimed, cairo 55.

HARFBUZZ = base("harfbuzz-11", "b")


def with_harfbuzz() -> Bundle:
    """An untimed second member of depth 2, needing libpng only."""
    made = font_bundle()
    freetype = next(d for d in made.derivations if d.drv == FREETYPE)
    harfbuzz = freetype.model_copy(update={"drv": HARFBUZZ, "time": None})
    return made.model_copy(update={"derivations": [*made.derivations, harfbuzz]})


def test_the_whole_chain_fits_an_hour() -> None:
    made = plan(font_bundle())
    assert (made.depth, made.estimate, made.untimed) == (4, 38 + 7 + 60 + 55, 1)
    assert made.builds == [LIBPNG, FREETYPE, FONTCONFIG, CAIRO]
    assert made.skipped == []


def test_a_tight_budget_stops_at_the_deepest_depth_that_fits() -> None:
    made = plan(font_bundle(), budget=100)
    assert (made.depth, made.estimate) == (2, 45)
    assert made.skipped == [FONTCONFIG, CAIRO]
    assert made.total_estimate == 160


def test_part_of_a_depth_gets_in_cheapest_first() -> None:
    made = plan(with_harfbuzz(), budget=50)
    assert made.builds == [LIBPNG, FREETYPE]
    assert made.depth == 1
    assert HARFBUZZ in made.skipped


def test_a_derivation_waits_for_its_changed_inputs() -> None:
    """cairo's 55 s fit after the first two, fontconfig's 100 s do not, and cairo needs it."""
    made = plan(font_bundle(), budget=120, untimed_seconds=100)
    assert made.builds == [LIBPNG, FREETYPE]
    assert made.skipped == [FONTCONFIG, CAIRO]


def test_the_untimed_guess_moves_the_cut() -> None:
    assert plan(font_bundle(), budget=100, untimed_seconds=10).depth == 3


def test_nothing_fits_when_depth_one_is_over_budget() -> None:
    made = plan(font_bundle(), budget=10)
    assert (made.depth, made.estimate, made.builds) == (0, 0, [])


def test_installables_name_every_output() -> None:
    assert plan(font_bundle(), budget=40).installables == [f"/nix/store/{LIBPNG}^*"]


def test_plan_round_trips() -> None:
    made = plan(font_bundle())
    assert Plan.model_validate_json(made.model_dump_json()) == made
