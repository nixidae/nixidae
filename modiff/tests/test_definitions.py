"""The Definitions section: what the edits touched first, the rest folded."""

from __future__ import annotations

import io

from rich.console import Console

from modiff.app import UNCHANGED_SHOWN, definitions
from modiff.model import Definition, OptionView, edit_file

MODULES = [Definition(f"/m/module-{i}.nix", f"[ {i} ]") for i in range(UNCHANGED_SHOWN + 2)]
EDIT = Definition(edit_file(("xs",)), "[ 99 ]")


def text(view: OptionView, all_shown: bool = False) -> str:
    console = Console(width=200, record=True, file=io.StringIO())
    for part in definitions(view, all_shown):
        console.print(part)
    return console.export_text()


def heads(shown: str) -> list[str]:
    """The line naming each definition shown in full."""
    # A value is indented four spaces; a head is a mark, a space, a name.
    return [line for line in shown.splitlines() if line[:2] in {"~ ", "- ", "+ ", "  "} and line[2:3] not in {"", " "}]


def test_the_edits_definitions_come_first_and_the_rest_fold() -> None:
    # A module can define an option twice: the fold counts it once.
    again = Definition(MODULES[-1].file, "[ 5 ]")
    old = (*MODULES, again, Definition("/m/gone.nix", "[ 0 ]"))
    view = OptionView(("xs",), defs_old=old, defs_new=(*MODULES, again, EDIT), new="[ … ]")
    shown = text(view)
    assert heads(shown) == [
        "- /m/gone.nix",
        f"+ {edit_file(('xs',))}",
        *(f"  /m/module-{i}.nix" for i in range(UNCHANGED_SHOWN)),
    ]
    assert "… 3 more the edits left alone, from 2 files: D shows them" in shown


def test_a_definition_whose_value_changed_shows_as_one_diff() -> None:
    old = (Definition("/m/users.nix", "[\n  1\n  2\n]"), *MODULES[:1])
    new = (*MODULES[:1], EDIT, Definition("/m/users.nix", "[\n  1\n  3\n]"))
    shown = text(OptionView(("xs",), defs_old=old, defs_new=new, new="[ … ]"))
    assert heads(shown) == ["~ /m/users.nix", f"+ {edit_file(('xs',))}", "  /m/module-0.nix"]
    lines = [line.replace(" ", "") for line in shown.splitlines()]
    assert [line for line in lines if line in {"-2", "+3", "-1", "+1"}] == ["-2", "+3"]


def test_all_shown_lists_every_definition() -> None:
    view = OptionView(("xs",), defs_old=tuple(MODULES), defs_new=(*MODULES, EDIT), new="[ … ]")
    shown = text(view, all_shown=True)
    assert len(heads(shown)) == len(MODULES) + 1
    assert "more the edits left alone" not in shown


def test_an_option_the_base_never_read_folds_too() -> None:
    view = OptionView(("xs",), defs_new=(*MODULES, EDIT), new="[ … ]", base_read=False)
    shown = text(view)
    assert heads(shown)[0] == f"+ {edit_file(('xs',))}"
    assert len(heads(shown)) == 1 + UNCHANGED_SHOWN
    # Nothing was compared, so the fold claims nothing about the edits.
    assert "… 2 more definitions, from 2 files" in shown
