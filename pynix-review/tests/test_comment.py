from __future__ import annotations

from test_build import made, outcomes
from test_bundle import CAIRO, FONTCONFIG, font_bundle

from pynix_review.build import Outcome, UnchangedFailure
from pynix_review.bundle import TIMED_OUT, UNCHANGED_INPUT_FAILED
from pynix_review.comment import LIMIT, LINK_LIMIT, LISTED, comment

VIEW = "https://example.invalid/view"


def test_comment_counts_and_links_the_graph() -> None:
    text = comment(made(), font_bundle(), VIEW)
    assert text.startswith("### pynix-review build\n")
    assert f"[Graph]({VIEW})" in text
    assert "| built | 1: builders took 40 s, estimated 38 s |" in text
    assert "| already built or substituted | 1 |" in text
    assert "| failed | 2 |" in text
    assert "| failed on an unchanged input | 0 |" in text


def test_a_link_with_the_dot_in_it_is_left_to_the_url_file() -> None:
    text = comment(made(), font_bundle(), "https://example.invalid/#" + "x" * LINK_LIMIT)
    assert "[Graph]" not in text
    assert "The graph's link is in `built.url`." in text


def test_nothing_built_says_no_times() -> None:
    text = comment(made(outcomes()[1:]), font_bundle(), VIEW)
    assert "| built | 0 |" in text


def test_the_change_s_failures_come_with_their_log_tail() -> None:
    failed = Outcome(FONTCONFIG, "permanent-failure", False, 1, 2, "error: x", log_tail="```\nmake: *** Error 1")
    text = comment(made([*outcomes()[:2], failed]), font_bundle(), VIEW)
    assert "#### Failed\n\n- `fontconfig` (permanent-failure after 1 s): error: x" in text
    assert "  ````\n  ```\n  make: *** Error 1\n  ````" in text


def test_base_failures_and_timeouts_are_not_the_change_s() -> None:
    found = [
        *outcomes()[:2],
        Outcome(FONTCONFIG, TIMED_OUT, False, 0, 0, "error: timed out after 5 seconds"),
        Outcome(CAIRO, UNCHANGED_INPUT_FAILED, False, 0, 0, "error: Cannot build"),
    ]
    text = comment(made(found), font_bundle(), VIEW)
    assert "#### Failed\n" not in text
    assert "| out of budget | 1 (1 timed out) |" in text
    assert "#### Failed on an unchanged input" in text
    assert "- `cairo` (unchanged-input-failed): error: Cannot build" in text


def test_the_unchanged_derivation_that_failed_is_named_with_its_log() -> None:
    found = [*outcomes()[:2], Outcome(CAIRO, UNCHANGED_INPUT_FAILED, False, 0, 0, "error: Cannot build")]
    culprit = UnchangedFailure(drv="ilw9-django.drv", message="error: builder failed", log_tail="FAILED test_x")
    text = comment(made(found).model_copy(update={"unchanged_failures": [culprit]}), font_bundle(), VIEW)
    assert "Unchanged derivations whose builder failed:\n\n- `ilw9-django.drv`: error: builder failed" in text
    assert "  FAILED test_x" in text


def test_a_long_list_is_counted_and_a_huge_comment_fits() -> None:
    tail = "x" * 20_000
    found = [Outcome(f"{n}-x.drv", "permanent-failure", False, 0, 0, "error: x", log_tail=tail) for n in range(40)]
    text = comment(made(found), font_bundle(), VIEW)
    assert f"- and {40 - LISTED} more" in text
    assert len(text) <= LIMIT
    assert "log tail of" not in text
