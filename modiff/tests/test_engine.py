from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from toy import SOURCE

from modiff.engine import Crash, Engine, EngineThread
from modiff.model import Cascade, Change, Edit, Kind, OptionView, Quiet, format_path, parse_option_path


@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[EngineThread]:
    # A real store: printing a derivation's path writes its .drv, which
    # `dummy://` refuses.
    root = tmp_path_factory.mktemp("store")
    thread = EngineThread(SOURCE, store=f"local?root={root}")
    loaded = thread.submit(Engine.load).result()
    assert not isinstance(loaded, Crash), loaded
    assert loaded.error is None
    yield thread
    thread.shutdown()


def run[T](thread: EngineThread, job: Callable[[Engine], T]) -> T:
    result = thread.submit(job).result()
    assert not isinstance(result, Crash), result.text
    return result


def cascade(thread: EngineThread, *edits: Edit) -> Cascade:
    return run(thread, lambda e: e.apply(list(edits)))


def changes(c: Cascade) -> dict[str, Change]:
    return {format_path(ch.path): ch for g in c.groups for ch in g.changes}


def test_edit_cascades_to_dependents(engine: EngineThread) -> None:
    c = cascade(engine, Edit(("a",), "2"))
    assert c.error is None
    got = changes(c)
    assert got["a"].old == "1" and got["a"].new == "2"
    assert got["b"].old == "10" and got["b"].new == "20"
    assert got["drv"].new == "«name toy-20»"
    assert "c" not in got


def test_changes_group_under_their_option(engine: EngineThread) -> None:
    c = cascade(engine, Edit(("flags",), "lib.mkForce { p = true; q = false; }"))
    groups = {g.option: [format_path(ch.path) for ch in g.changes] for g in c.groups}
    assert groups == {("flags",): ["flags.q"]}


def test_shared_true_does_not_hide_a_change(engine: EngineThread) -> None:
    """Nix shares one value for every `true`: a global seen set skipped `q`."""
    got = changes(cascade(engine, Edit(("flags",), "lib.mkForce { p = true; q = false; }")))
    assert got["flags.q"].old == "true" and got["flags.q"].new == "false"


def test_list_insert_is_aligned(engine: EngineThread) -> None:
    got = changes(cascade(engine, Edit(("lines",), 'lib.mkBefore [ "zero" ]')))
    assert set(got) == {"lines[0]"}
    assert got["lines[0]"].kind is Kind.ADDED and got["lines[0]"].new == "zero"


INSERTED_CHECK = 'lib.mkOrder 750 [ { name = "inserted"; ok = true; msg = throw "never read"; } ]'


def test_attrset_insert_aligns_on_literal_fields(engine: EngineThread) -> None:
    """Every element is an attrset: keyed on kind alone, the insert shifted
    each later element onto its neighbour and changed its `name`."""
    c = cascade(engine, Edit(("checks",), INSERTED_CHECK))
    assert c.error is None
    got = changes(c)
    assert set(got) == {"checks[1]"}
    assert got["checks[1]"].kind is Kind.ADDED
    assert 'name = "inserted";' in got["checks[1]"].new


def test_aligning_leaves_unforced_fields_unforced(engine: EngineThread) -> None:
    c = cascade(engine, Edit(("checks",), INSERTED_CHECK))
    assert not [ch for g in c.groups for ch in g.changes if ch.kind is Kind.FAILED]
    view = run(engine, lambda e: e.explain(("checks",)))
    assert "«thunk»" in view.new
    assert "never read" not in view.new


def test_type_error_is_a_failed_row(engine: EngineThread) -> None:
    c = cascade(engine, Edit(("a",), '"two"'))
    assert c.error is None
    failed = [ch for g in c.groups for ch in g.changes if ch.kind is Kind.FAILED]
    assert len(failed) == 1, failed
    assert "modiff edit: a" in failed[0].new


def test_parse_error_names_the_edit(engine: EngineThread) -> None:
    c = cascade(engine, Edit(("a",), "1 +"))
    assert c.error is not None and c.error.startswith("a: ")


def quiet(c: Cascade) -> dict[str, Quiet | None]:
    return {format_path(g.option): g.quiet for g in c.groups if not g.changes}


def test_edit_nothing_reads_is_unread(engine: EngineThread) -> None:
    """The toy never forces `c`."""
    assert quiet(cascade(engine, Edit(("c",), '"x"'))) == {"c": Quiet.UNREAD}


def test_edit_to_the_same_value_is_same(engine: EngineThread) -> None:
    assert quiet(cascade(engine, Edit(("a",), "1"))) == {"a": Quiet.SAME}


def test_edit_a_definition_outranks_is_outranked(engine: EngineThread) -> None:
    """`b` has a plain definition, priority 100; mkDefault is 1000."""
    assert quiet(cascade(engine, Edit(("b",), "lib.mkDefault 3"))) == {"b": Quiet.OUTRANKED}


def test_an_edit_that_changes_something_is_not_quiet(engine: EngineThread) -> None:
    c = cascade(engine, Edit(("a",), "2"), Edit(("c",), '"x"'))
    assert quiet(c) == {"c": Quiet.UNREAD}
    assert all(g.quiet is None for g in c.groups if g.changes)


def test_disabled_edit_changes_nothing(engine: EngineThread) -> None:
    assert cascade(engine, Edit(("a",), "2", enabled=False)).groups == ()


def test_explain_shows_the_edit_as_a_definition(engine: EngineThread) -> None:
    def job(e: Engine) -> OptionView:
        e.apply([Edit(("a",), "2")])
        return e.explain(("a",))

    view = run(engine, job)
    assert view.type_description == "signed integer"
    assert view.description == "The input."
    assert view.old == "1" and view.new == "2"
    # The default is a definition too, at mkOptionDefault priority: it is the
    # only one before the edit, and the edit outranks it after.
    assert [d.value for d in view.defs_old] == ["1"]
    assert [d.file for d in view.defs_new] == ["modiff edit: a"]


def test_lazy_value_shows_thunks(engine: EngineThread) -> None:
    """`c` was never needed: Nix's printer says so instead of evaluating it."""
    view = run(engine, lambda e: e.explain(("c",)))
    assert view.old == "«thunk»"
    assert view.default == '"untouched"'


def test_default_is_evaluated_in_the_extended_configuration(engine: EngineThread) -> None:
    """A list literal is a thunk until something reads it. With no edit,
    only the base exists, and forcing there would widen later cascades."""

    def job(e: Engine) -> tuple[str, str]:
        e.apply([])
        lazy = e.explain(("ports",)).default
        e.apply([Edit(("c",), '"x"')])
        return lazy, e.explain(("ports",)).default

    assert run(engine, job) == ("«thunk»", "[ 22 ]")


def test_explain_evaluates_nothing(engine: EngineThread) -> None:
    """Each `msg` reads another option. Showing `checks` must not evaluate
    them, or `c` counts as evaluated in every later cascade."""
    run(engine, lambda e: e.explain(("checks",)))
    assert run(engine, lambda e: e.explain(("c",))).old == "«thunk»"
    assert "c" not in changes(cascade(engine, Edit(("c",), '"edited"')))


def test_definitions_print_derivations_by_what_was_evaluated(engine: EngineThread) -> None:
    """The toy forces `drv.name` and no path."""
    view = run(engine, lambda e: e.explain(("drv",)))
    assert [d.value for d in view.defs_old] == ["«derivation toy-10»"]


def test_option_paths(engine: EngineThread) -> None:
    paths = run(engine, Engine.option_paths)
    assert {("a",), ("b",), ("c",), ("flags",), ("lines",), ("drv",)} <= set(paths)


def test_parse_option_path_round_trips() -> None:
    path = ("environment", "etc", "pam.d/sshd", "text")
    assert parse_option_path(format_path(path)) == path
