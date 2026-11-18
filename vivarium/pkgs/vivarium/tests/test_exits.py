"""One table of exit statuses, and the help texts read from it. nixidae/nixidae#1483."""

from __future__ import annotations

from vivarium.exits import LABELS, Exit, describe


def test_each_status_has_one_number_and_a_label() -> None:
    assert set(LABELS) == set(Exit)
    assert len({status.value for status in Exit}) == len(Exit)


def test_describe_names_every_status_in_order() -> None:
    assert describe() == "0 passed, 1 failed, 2 no verdict, 3 no run, 4 paused, 5 stopped, 6 ready, 7 refused"
    assert describe(Exit.PAUSED, Exit.READY) == "4 paused, 6 ready"


def test_a_usage_error_shares_its_number_with_no_verdict() -> None:
    """argparse exits 2; the table says so instead of hiding it."""
    assert Exit.USAGE is Exit.NO_VERDICT
