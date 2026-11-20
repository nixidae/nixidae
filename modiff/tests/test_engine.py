from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final

import huggorm
import pytest
from toy import SOURCE, TOY

from modiff.engine import Crash, Engine, EngineThread, Source, edit_module, nixos_source
from modiff.model import (
    Cascade,
    Change,
    DrvDiff,
    Edit,
    Kind,
    OptionView,
    Quiet,
    Rebuild,
    blamed_path,
    edited_path,
    edits_module,
    expand_strings,
    format_path,
    pair_drvs,
    parse_option_path,
    priority_text,
    store_name,
)
from modiff.walk import PRINT_DEPTH, PRINT_ITEMS, PRINT_STRING, LazyPrinter, count_forced, other_paths


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


def test_rebuilds_include_a_derivation_nothing_coerced(engine: EngineThread) -> None:
    """`top` is forced by its drvPath alone, as NixOS forces the toplevel:
    no coercion records it, only its input `dep`."""
    c = cascade(engine, Edit(("a",), "2"))
    assert sorted(store_name(p) for p in c.drvs_added) == ["dep-20", "top", "unit", "unit"]
    assert sorted(store_name(p) for p in c.drvs_removed) == ["dep-10", "top", "unit", "unit"]


CA: Final = Source(
    """
    let lib = import <nixpkgs/lib>; in lib.evalModules {
      modules = [ ({ config, ... }: {
        options.v = lib.mkOption { type = lib.types.int; default = 1; };
        options.top = lib.mkOption { type = lib.types.package; };
        config.top = let
          cav = derivation {
            name = "cav";
            system = "x86_64-linux";
            builder = "/bin/sh";
            outputs = [ "out" "dev" ];
            v = config.v;
            __contentAddressed = true;
            outputHashMode = "recursive";
            outputHashAlgo = "sha256";
          };
        in derivation {
          name = "top";
          system = "x86_64-linux";
          builder = "/bin/sh";
          # Two placeholders, one `.drv`.
          inputs = [ cav cav.dev ];
        };
      }) ];
    }
    """,
    (("config", "top", "drvPath"),),
)


def test_a_content_addressed_derivation_pairs_by_its_name(tmp_path: Path) -> None:
    """Its output path is a placeholder until it is built: `/0z17…`, no name."""
    features = huggorm.get_setting("experimental-features") or ""
    huggorm.set_setting("experimental-features", f"{features} ca-derivations")
    thread = EngineThread(CA, store=f"local?root={tmp_path}")
    try:
        run(thread, Engine.load)
        c = run(thread, lambda e: e.apply([Edit(("v",), "2")]))
    finally:
        thread.shutdown()
        huggorm.set_setting("experimental-features", features)
    assert [(d.kind, d.name) for d in c.drvs] == [(Rebuild.REBUILT, "cav"), (Rebuild.REBUILT, "top")]


def test_a_derivation_the_edit_drops_is_removed(engine: EngineThread) -> None:
    """Its source goes with it, folded under it."""
    c = cascade(engine, Edit(("extras",), "[ ]"))
    gone = [d for d in c.drvs if d.name != "top"]
    assert [(d.kind, d.name, [store_name(p) for p in d.only]) for d in gone] == [
        (Rebuild.REMOVED, "extra", ["extra-src"])
    ]


def test_listing_rebuilds_forces_nothing_in_the_base(engine: EngineThread) -> None:
    """The base forced `top.drvPath` alone. Reading its `outPath` for the
    rebuilds would key `top` by outPath in every later cascade."""
    first = changes(cascade(engine, Edit(("a",), "2")))["top"]
    second = changes(cascade(engine, Edit(("a",), "2")))["top"]
    assert first == second
    assert first.old.startswith("«drvPath ")


def test_drv_diff_names_the_input_that_changed(engine: EngineThread) -> None:
    def job(e: Engine) -> DrvDiff:
        c = e.apply([Edit(("a",), "2")])
        top = next(d for d in pair_drvs(c.drvs_added, c.drvs_removed) if d.name == "top")
        assert top.old is not None and top.new is not None
        return e.drv_diff(top.old, top.new)

    diff = run(engine, job)
    assert diff.error is None, diff.error
    assert [(d.kind, d.name) for d in diff.inputs] == [
        (Rebuild.VERSION, "dep-20"),
        (Rebuild.VERSION, "hidden-20"),
        (Rebuild.REBUILT, "unit"),
        (Rebuild.REBUILT, "unit"),
    ]
    assert [a.name for a in diff.attrs] == ["env.hidden", "env.inputs"]


def test_derivations_of_one_name_pair_most_alike(engine: EngineThread) -> None:
    """Paired wrongly, each `unit` would differ in `which` as well."""

    def job(e: Engine) -> list[list[str]]:
        c = e.apply([Edit(("a",), "2")])
        units = [d for d in c.drvs if d.name == "unit"]
        assert len(units) == 2
        found = []
        for unit in units:
            assert unit.old is not None and unit.new is not None
            found.append([a.name for a in e.drv_diff(unit.old, unit.new).attrs])
        return found

    assert run(engine, job) == [["env.b"], ["env.b"]]


def test_drv_diff_reads_an_input_by_its_drv_file(engine: EngineThread) -> None:
    """An input that has no row is known only by its `.drv` path."""

    def job(e: Engine) -> DrvDiff:
        c = e.apply([Edit(("a",), "2")])
        top = next(d for d in pair_drvs(c.drvs_added, c.drvs_removed) if d.name == "top")
        assert top.old is not None and top.new is not None
        hidden = next(i for i in e.drv_diff(top.old, top.new).inputs if i.name == "hidden-20")
        assert hidden.old is not None and hidden.new is not None
        assert hidden.new.startswith("/")
        return e.drv_diff(hidden.old, hidden.new)

    diff = run(engine, job)
    assert diff.error is None, diff.error
    assert [a.name for a in diff.attrs] == ["env.name"]


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


def test_submodule_insert_aligns_on_the_fields_the_base_forced(engine: EngineThread) -> None:
    """A submodule's fields are thunks on the new side, so no field is
    known there: by position, every person shifted onto the next."""
    c = cascade(engine, Edit(("people",), 'lib.mkBefore [ { name = "zed"; uid = 9; } ]'))
    assert c.error is None
    got = changes(c)
    assert set(got) == {"people[0]"}
    assert got["people[0]"].kind is Kind.ADDED
    assert 'name = "zed";' in got["people[0]"].new


def test_aligning_forces_no_field_the_base_left_a_thunk(engine: EngineThread) -> None:
    """`fourth` has a literal `msg`; the other `msg`s are thunks the base
    never forced. Forcing them on the new side makes every base definition
    differ from itself."""
    cascade(engine, Edit(("checks",), INSERTED_CHECK))
    view = run(engine, lambda e: e.explain(("checks",)))
    assert set(view.defs_old) <= set(view.defs_new)


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


def test_a_conflict_fails_under_the_option_it_names(engine: EngineThread) -> None:
    c = cascade(engine, Edit(("z",), "4"))
    assert c.error is None
    failed = [(g.option, ch) for g in c.groups for ch in g.changes if ch.kind is Kind.FAILED]
    assert len(failed) == 1, failed
    option, change = failed[0]
    assert option == ("z",)
    assert change.path == ("z",)
    assert change.via == ("checks", 0, "ok")
    assert "conflicting definition values" in change.new


@pytest.mark.parametrize(
    ("expr", "message"),
    [
        ('lib.mkForce (throw "boom")', "boom"),
        ('lib.mkForce (abort "stop")', "evaluation aborted"),
        ("lib.mkForce (assert false; 1)", "assertion 'false' failed"),
        ("lib.mkForce (let x = x + 1; in x)", "infinite recursion"),
        ("lib.mkForce { }.missing", "attribute 'missing' missing"),
    ],
)
def test_a_failing_edit_fails_under_its_own_option(engine: EngineThread, expr: str, message: str) -> None:
    """The messages name no option, and the walk meets each first in
    `checks`, which sorts first and reads `z`."""
    before = cascade(engine, Edit(("a",), "2"))
    c = cascade(engine, Edit(("z",), expr))
    failed = [(g.option, ch) for g in c.groups for ch in g.changes if ch.kind is Kind.FAILED]
    assert len(failed) == 1, failed
    option, change = failed[0]
    assert (option, change.path, change.via) == (("z",), ("z",), ("checks", 0, "ok"))
    assert message in change.new
    assert cascade(engine, Edit(("a",), "2")).groups == before.groups


def test_a_failing_edit_beside_a_valid_one_fails_under_its_own_option(engine: EngineThread) -> None:
    """`checks` is edited too, and the walk meets the failure first inside it."""
    c = cascade(engine, Edit(("checks",), "lib.mkAfter [ ]"), Edit(("z",), 'lib.mkForce (throw "boom")'))
    failed = [ch for g in c.groups for ch in g.changes if ch.kind is Kind.FAILED]
    assert [(ch.path, ch.via) for ch in failed] == [(("z",), ("checks", 0, "ok"))]


def test_edited_path_prefers_an_edited_path_to_one_inside_an_edit() -> None:
    failed: list[tuple[str | int, ...]] = [("checks", 0, "ok"), ("z", "x"), ("z",)]
    assert edited_path(failed, [("a",), ("z",)]) == ("z",)
    assert edited_path(failed, [("z", "x"), ("checks",)]) == ("z", "x")
    assert edited_path(failed[:2], [("z",)]) == ("z", "x")
    assert edited_path(failed, [("a",)]) is None


def test_a_failed_merge_leaves_the_other_definitions_alone(engine: EngineThread) -> None:
    """`1` fails the merge of all of `checks`, so the walk forces nothing
    in it; the base's definitions still compare equal. (`[ 1 ]` fails only
    its element: `listOf` merges each element lazily.)"""
    c = cascade(engine, Edit(("checks",), "1"))
    failed = [(g.option, ch) for g in c.groups for ch in g.changes if ch.kind is Kind.FAILED]
    assert [(option, ch.path) for option, ch in failed] == [(("checks",), ("checks",))]
    view = run(engine, lambda e: e.explain(("checks",)))
    assert set(view.defs_old) <= set(view.defs_new)
    assert [d.file for d in view.defs_new if d not in view.defs_old] == ["modiff edit: checks"]
    assert view.new.startswith("«error: ")
    # The second visit skips forcing and shows the same.
    assert run(engine, lambda e: e.explain(("checks",))) == view


def test_elements_the_base_never_read_stay_unread(engine: EngineThread) -> None:
    """The base forced the list `many` and none of its elements."""
    c = cascade(engine, Edit(("many",), "lib.mkForce (lib.genList (i: if i == 200 then 0 else i) 250)"))
    assert quiet(c) == {"many": Quiet.PARTIAL}
    view = run(engine, lambda e: e.explain(("many",)))
    assert view.new == view.old


def test_a_value_the_base_failed_is_no_failure_of_the_edit(engine: EngineThread) -> None:
    c = cascade(engine, Edit(("a",), "2"))
    assert not [ch for g in c.groups for ch in g.changes if ch.kind is Kind.FAILED]
    assert ("tried",) not in {g.option for g in c.groups}


def test_a_failed_part_prints_as_failed(engine: EngineThread) -> None:
    cascade(engine, Edit(("z",), "4"))
    view = run(engine, lambda e: e.explain(("checks",)))
    assert "ok = «failed»;" in view.new
    assert "«failed»" not in view.old
    assert any("ok = «failed»;" in d.value for d in view.defs_new)


PLAIN: Final = r"""{
  "0x" = 1;
  "a.b" = 2;
  "" = 3;
  "or" = 4;
  "x'" = 5;
  neg = -3;
  path = /tmp/modiff;
  primop = builtins.map;
  dollars = "$${x} $$ $";
  a = 1;
  b = [ 1 2 { c = "x\ny"; } ];
  "d e" = { };
  type = "t";
  f = [ ];
  g = { h = [ 3 ]; };
  i = 1.5;
  j = null;
  k = true;
  l = x: x;
  deep = { a = { b = { c = { d = { e = { f = 1; }; }; }; }; }; };
  s = "q \"''\" \${v} \\ \t";
  "if" = [ [ ] ];
  m = [ { n = 1; } ];
  long = builtins.concatStringsSep "" (builtins.genList (i: "x") 5000);
}"""


@pytest.mark.parametrize("items", [2000, 5, 1, 0])
def test_the_printer_lays_out_values_as_nix_does(engine: EngineThread, items: int) -> None:
    """On a fully evaluated value with no derivation, Nix's printer forces
    nothing, so both must print the same."""

    def job(e: Engine) -> tuple[str, str]:
        v = e.state.eval_expr(f"let v = {PLAIN}; in builtins.deepSeq v v", "/")
        e.state.force(v)
        nix = v.print_value(
            max_depth=PRINT_DEPTH,
            max_attrs=items,
            max_list_items=items,
            max_string_length=PRINT_STRING,
            pretty_indent=2,
        )
        return LazyPrinter(items).show(v), nix

    ours, nix = run(engine, job)
    assert ours == nix


WRAPPED: Final = r"""let lib = import <nixpkgs/lib>; v = {
  a = lib.mkForce "x";
  b = lib.mkOverride 70 1;
  c = lib.mkBefore [ 1 ];
  d = lib.mkIf true { e = 1; };
  f = lib.mkMerge [ (lib.mkDefault (-3)) 2 ];
  g = lib.mkForce (lib.mkDefault 1);
}; in builtins.deepSeq v v"""


def test_a_definition_wrapper_prints_as_its_lib_call(engine: EngineThread) -> None:
    def job(e: Engine) -> str:
        v = e.state.eval_expr(WRAPPED, "/")
        e.state.force(v)
        return LazyPrinter(PRINT_ITEMS).show(v)

    assert run(engine, job) == "\n".join(
        [
            "{",
            '  a = lib.mkForce "x";',
            "  b = lib.mkOverride 70 1;",
            "  c = lib.mkBefore [ 1 ];",
            "  d = lib.mkIf true { e = 1; };",
            "  f = lib.mkMerge [",
            "    (lib.mkDefault (-3))",
            "    2",
            "  ];",
            "  g = lib.mkForce (lib.mkDefault 1);",
            "}",
        ]
    )


def test_a_change_shows_once_when_an_option_holds_config(engine: EngineThread) -> None:
    """`alias.cfg` is `config`: `b` changes at `b`, not again under it."""
    c = cascade(engine, Edit(("a",), "2"))
    assert "b" in changes(c)
    assert not any(g.option == ("alias",) for g in c.groups)


def test_the_walk_compares_a_set_reached_by_many_paths_once(engine: EngineThread) -> None:
    """The toy's `wide` has 2^22 paths and 23 sets."""
    c = cascade(engine, Edit(("a",), "2"))
    assert c.visited < 1000


def test_a_changed_set_reached_by_many_paths_shows_once(engine: EngineThread) -> None:
    """The toy's `fan` changes at all 2^22 paths. Its set at `fan.b` is the
    one at `fan.a`: the walk reports it once and points there from `b`."""
    c = cascade(engine, Edit(("seed",), "1"))
    fan = [ch for g in c.groups if g.option == ("fan",) for ch in g.changes]
    assert c.visited < 1000
    assert [ch for ch in fan if ch.kind is not Kind.SAME] == [
        Change(Kind.CHANGED, ("fan", *("a",) * 22, "x"), "0", "1")
    ]
    assert len(fan) == 23
    assert Change(Kind.SAME, ("fan", "b"), via=("fan", "a")) in fan


def test_count_forced_counts_a_shared_set_once(engine: EngineThread) -> None:
    """Each level holds the one below twice: 2^20 paths to the last."""
    expr = "builtins.foldl' (s: _: builtins.deepSeq s { a = s; b = s; }) { x = 1; } (builtins.genList (i: i) 20)"

    def job(e: Engine) -> int:
        v = e.state.eval_expr(f"let v = {expr}; in builtins.deepSeq v v", "/")
        e.state.force(v)
        return count_forced(v)

    assert run(engine, job) == 22


def test_a_shared_value_prints_in_full_each_time(engine: EngineThread) -> None:
    """Nix prints a set it printed before as «repeated», so which side of a
    diff says «repeated» depends on attribute order."""

    def job(e: Engine) -> tuple[str, str]:
        v = e.state.eval_expr("let s = { z = 1; }; in builtins.deepSeq s { a = s; b = [ s ]; }", "/")
        e.state.force(v)
        e.state.force(v.get("b"))
        return LazyPrinter(PRINT_ITEMS).show(v), v.print_value(pretty_indent=2)

    ours, nix = run(engine, job)
    assert "«repeated»" in nix
    assert ours.count("{ z = 1; }") == 2


def test_explaining_every_option_widens_no_cascade(engine: EngineThread) -> None:
    """Printing and listing definitions force nothing in the base."""
    edits = (Edit(("a",), "2"), Edit(("people",), '[ { name = "dave"; uid = 4; } ]'))
    before = cascade(engine, *edits)
    for option in run(engine, Engine.option_paths):
        run(engine, lambda e, option=option: e.explain(option))
    assert cascade(engine, *edits).groups == before.groups


def test_no_action_widens_the_next_cascade(engine: EngineThread) -> None:
    """Every job the app submits: explain (moving, `/`, `D`), the option
    list, `freeform` and `syntax_error` (`/`, tab completion, the edit dialog), a
    derivation's diff, and a second edit turned on and off. None may
    change the cascade of the first edit."""
    edit = Edit(("a",), "2")
    before = cascade(engine, edit)
    run(engine, Engine.freeform)
    run(engine, lambda e: e.syntax_error("config.a + 1"))
    for drv in before.drvs:
        if drv.old is not None and drv.new is not None:
            run(engine, lambda e, drv=drv: e.drv_diff(drv.old, drv.new))
    cascade(engine, edit, Edit(("c",), '"x"'), Edit(("checks",), "1"))
    for option in run(engine, Engine.option_paths):
        run(engine, lambda e, option=option: (e.explain(option), e.option_of(option)))
    after = cascade(engine, edit)
    assert after.groups == before.groups
    assert after.drvs == before.drvs


def test_an_edit_that_forces_a_shared_value_further_changes_no_later_cascade(engine: EngineThread) -> None:
    """`half` comes from outside the modules, so both sides hold the one
    value, and forcing `half.q` for the edited side forces it in the base
    too. The walk skips a value both sides share: no cascade shows it."""
    edit = Edit(("a",), "2")
    before = cascade(engine, edit)
    picked = cascade(engine, Edit(("pick",), '"q"'))
    assert changes(picked)["picked"] == Change(Kind.CHANGED, ("picked",), "1", "2")
    assert "half" not in changes(picked)
    after = cascade(engine, edit)
    assert after.groups == before.groups
    assert after.drvs == before.drvs


def test_other_paths_counts_the_rest() -> None:
    assert other_paths(0) == ""
    assert other_paths(1) == "1 other path"
    assert other_paths(80) == "80 other paths"


def test_blamed_path_reads_the_module_system_messages() -> None:
    assert blamed_path("The option `a.b' has conflicting definition values:") == ("a", "b")
    assert blamed_path('A definition for option `users.users."x.y".uid\' is not of type') == (
        "users",
        "users",
        "x.y",
        "uid",
    )
    assert blamed_path("The option `a.b` is defined both as `x` and `y`") == ("a", "b")
    assert blamed_path("attribute 'x' missing") is None
    assert blamed_path("Failed assertions:\n- Set the option `a.b' first.") is None


def test_parse_error_names_the_edit(engine: EngineThread) -> None:
    c = cascade(engine, Edit(("a",), "lib.mkForce ("))
    assert c.error is not None and c.error.startswith("a: ")
    # Nix's frame shows the edit, not the module modiff wraps it in.
    assert "lib.mkForce (" in c.error
    assert "setAttrByPath" not in c.error
    assert "with" not in c.error


def test_a_failing_force_path_is_named(tmp_path: Path) -> None:
    thread = EngineThread(Source(TOY, (("config", "a"), ("config", "nope"))), store=f"local?root={tmp_path}")
    try:
        loaded = run(thread, Engine.load)
    finally:
        thread.shutdown()
    assert loaded.error is not None
    assert loaded.error.startswith("evaluating config.nope: attribute 'nope' missing")


def test_a_nixos_path_may_hold_a_space(engine: EngineThread) -> None:
    source = nixos_source("/tmp/my config/configuration.nix", "/tmp/my nixpkgs")
    run(engine, lambda e: e.state.parse_expr(source.expr, "/"))
    assert '"/tmp/my config/configuration.nix"' in source.expr


def test_an_edit_may_end_in_a_comment(engine: EngineThread) -> None:
    assert cascade(engine, Edit(("a",), "2 # two")).groups == cascade(engine, Edit(("a",), "2")).groups


def test_a_free_name_is_no_parse_error(engine: EngineThread) -> None:
    assert cascade(engine, Edit(("a",), "lib.mkForce (lib.max 1 2)")).error is None


def quiet(c: Cascade) -> dict[str, Quiet | None]:
    return {format_path(g.option): g.quiet for g in c.groups if not g.changes}


def test_edit_nothing_reads_is_unread(engine: EngineThread) -> None:
    """The toy never forces `c`."""
    assert quiet(cascade(engine, Edit(("c",), '"x"'))) == {"c": Quiet.UNREAD}


def test_edit_to_the_same_value_is_same(engine: EngineThread) -> None:
    assert quiet(cascade(engine, Edit(("a",), "1"))) == {"a": Quiet.SAME}


def test_an_outranked_edit_is_dropped(engine: EngineThread) -> None:
    """`b` has a plain definition, priority 100; mkDefault is 1000."""
    assert quiet(cascade(engine, Edit(("b",), "lib.mkDefault 3"))) == {"b": Quiet.DROPPED}


def test_an_edit_through_another_path_is_not_unread(engine: EngineThread) -> None:
    """A renamed option: the edit defines `a`, and `config.old` stays a
    thunk. The cascade is not empty, so the row claims nothing more."""
    c = cascade(engine, Edit(("old",), "2"))
    assert "b" in changes(c)
    assert quiet(c) == {"old": Quiet.ELSEWHERE}


def test_unforced_children_are_not_the_same_value(engine: EngineThread) -> None:
    """The base forced each `ok` and no `msg`: equal evaluated parts say
    nothing about the rest."""
    edit = Edit(
        ("checks",),
        'lib.mkForce (map (n: { name = n; ok = true; msg = "other"; }) [ "first" "second" "third" ]'
        ' ++ [ { name = "fourth"; ok = true; msg = "always";'
        ' tool = derivation { name = "tool"; system = "x86_64-linux"; builder = "/bin/sh"; }; } ])',
    )
    assert quiet(cascade(engine, edit)) == {"checks": Quiet.PARTIAL}


def test_an_edit_that_changes_something_is_not_quiet(engine: EngineThread) -> None:
    c = cascade(engine, Edit(("a",), "2"), Edit(("c",), '"x"'))
    assert quiet(c) == {"c": Quiet.ELSEWHERE}
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


def test_explain_shows_the_winning_priority(engine: EngineThread) -> None:
    """`a` has only its default, 1500; the edit is a plain definition, then
    mkForce."""

    def job(e: Engine, expr: str) -> OptionView:
        e.apply([Edit(("a",), expr)])
        return e.explain(("a",))

    plain = run(engine, lambda e: job(e, "2"))
    assert (plain.prio_old, plain.prio_new) == (1500, 100)
    forced = run(engine, lambda e: job(e, "lib.mkForce 2"))
    assert forced.prio_new == 50
    assert priority_text(50) == "50, lib.mkForce"
    assert priority_text(75) == "75, lib.mkOverride 75"


def test_lazy_value_shows_thunks(engine: EngineThread) -> None:
    """`c` was never needed: Nix's printer says so instead of evaluating it."""
    view = run(engine, lambda e: e.explain(("c",)))
    assert view.old == "«thunk»"
    assert view.default == '"untouched"'


def test_default_is_evaluated_in_the_extended_configuration(engine: EngineThread) -> None:
    """A list literal is a thunk until something reads it. With no edit,
    only the base exists, and forcing there would widen later cascades."""

    def job(e: Engine) -> tuple[str, str, str]:
        e.apply([])
        lazy = e.explain(("ports",)).default
        e.apply([Edit(("c",), '"x"')])
        forced = e.explain(("ports",)).default
        e.apply([])
        return lazy, forced, e.explain(("ports",)).default

    assert run(engine, job) == ("«thunk»", "[ 22 ]", "«thunk»")


def test_an_unread_option_lists_only_the_new_definitions(engine: EngineThread) -> None:
    def job(e: Engine) -> OptionView:
        e.apply([Edit(("ports",), "[ 2222 ]")])
        return e.explain(("ports",))

    view = run(engine, job)
    assert not view.base_read and view.defs_old == ()
    assert [d.file for d in view.defs_new] == ["modiff edit: ports"]


def test_explain_evaluates_nothing(engine: EngineThread) -> None:
    """Each `msg` reads another option. Showing `checks` must not evaluate
    them, or `c` counts as evaluated in every later cascade."""
    run(engine, lambda e: e.explain(("checks",)))
    assert run(engine, lambda e: e.explain(("c",))).old == "«thunk»"
    assert "c" not in changes(cascade(engine, Edit(("c",), '"edited"')))


def test_a_change_past_two_hundred_items_shows(engine: EngineThread) -> None:
    """Nix's print budget counts items across the whole value: at 200,
    the element the edit appends prints as «… items elided»."""
    cascade(engine, Edit(("many",), "lib.mkAfter [ 999 ]"))
    view = run(engine, lambda e: e.explain(("many",)))
    assert "999" in view.new
    assert "elided" not in view.new


def test_definitions_print_derivations_by_what_was_evaluated(engine: EngineThread) -> None:
    """The toy forces `drv.name` and no path."""
    view = run(engine, lambda e: e.explain(("drv",)))
    assert [d.value for d in view.defs_old] == ["«derivation toy-10»"]


FREEFORM: Final = Source(
    """
    let lib = import <nixpkgs/lib>; in lib.evalModules {
      modules = [ {
        freeformType = lib.types.attrsOf lib.types.anything;
        options.x = lib.mkOption { type = lib.types.int; default = 1; };
      } ];
    }
    """,
    (("config", "x"),),
)


def test_a_freeform_top_level_takes_any_path(tmp_path: Path) -> None:
    thread = EngineThread(FREEFORM, store=f"local?root={tmp_path}")
    try:
        assert not isinstance(run(thread, Engine.load), Crash)
        assert run(thread, Engine.freeform)
        c = run(thread, lambda e: e.apply([Edit(("undeclared",), "2")]))
        assert c.error is None
    finally:
        thread.shutdown()


def test_the_toy_is_not_freeform(engine: EngineThread) -> None:
    assert not run(engine, Engine.freeform)


def test_option_paths(engine: EngineThread) -> None:
    paths = run(engine, Engine.option_paths)
    assert {("a",), ("b",), ("c",), ("flags",), ("lines",), ("drv",)} <= set(paths)


def test_parse_option_path_round_trips() -> None:
    path = ("environment", "etc", "pam.d/sshd", "text")
    assert parse_option_path(format_path(path)) == path


@pytest.mark.parametrize(
    "edits",
    [
        [Edit(("a",), "2"), Edit(("c",), '"x"'), Edit(("lines",), "lib.mkAfter [ config.c ]")],
        # One path under another: in one attribute set, Nix refuses
        # `flags = lib.mkForce { … }; flags.p = …;`.
        [
            Edit(("flags",), "lib.mkForce { r = true; }"),
            Edit(("flags", "p"), "false"),
            Edit(("a",), "3", enabled=False),
        ],
        # A comment to the end of the line, then more definitions.
        [Edit(("a",), "2 # two"), Edit(("c",), '"x"')],
    ],
)
def test_the_written_module_means_what_the_edits_meant(engine: EngineThread, tmp_path: Path, edits: list[Edit]) -> None:
    module = tmp_path / "edits.nix"
    module.write_text(edits_module(edits))
    on = " ".join(f"({edit_module(e)})" for e in edits if e.enabled)

    def job(e: Engine) -> tuple[str, str]:
        def config(modules: str) -> str:
            # `tried.bad` throws by design; `wide` and `fan` are 2^22 values
            # as JSON; `alias` holds the configuration itself.
            expr = (
                f"builtins.toJSON (removeAttrs (({TOY}).extendModules {{ modules = [ {modules} ]; }}).config"
                ' [ "tried" "wide" "fan" "alias" ])'
            )
            return str(e.state.eval_expr(expr, "/").string_value())

        return config(str(module)), config(on)

    written, applied = run(engine, job)
    assert written == applied


def test_expanded_strings_evaluate_to_the_printed_value(engine: EngineThread) -> None:
    value = r"""{ a = "one\n  two\n"; b = [ "x\ty\r\n" "q \"''\" \${v} \\\n" ]; c.d = "last\nline"; }"""

    def job(e: Engine) -> tuple[str, bool]:
        printed = e.state.eval_expr(value, "/").print_value(force=True, pretty_indent=2)
        expanded = expand_strings(printed)
        same = e.state.eval_expr(f"({expanded}) == ({value})", "/")
        return expanded, bool(same.print_value() == "true")

    expanded, same = run(engine, job)
    assert "''" in expanded
    assert same, expanded
