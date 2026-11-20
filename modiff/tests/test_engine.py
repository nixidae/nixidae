from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from toy import SOURCE

from modiff.engine import Crash, Engine, EngineThread
from modiff.model import Cascade, Change, Edit, Kind, OptionView, format_path, parse_option_path


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


def test_type_error_is_a_failed_row(engine: EngineThread) -> None:
    c = cascade(engine, Edit(("a",), '"two"'))
    assert c.error is None
    failed = [ch for g in c.groups for ch in g.changes if ch.kind is Kind.FAILED]
    assert len(failed) == 1, failed
    assert "modiff edit: a" in failed[0].new


def test_parse_error_names_the_edit(engine: EngineThread) -> None:
    c = cascade(engine, Edit(("a",), "1 +"))
    assert c.error is not None and c.error.startswith("a: ")


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
