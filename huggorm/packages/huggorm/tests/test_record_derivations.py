"""A state records each derivation it coerces to its outPath
(nix-record-derivations.patch, nixidae/nixidae#1373).

An SBOM of what an evaluation ships starts from store paths in a
string. The record maps each back to the value that carried it, with
its `meta`.
"""

import pathlib
from typing import Any

import pytest

PKG = (
    'let pkg = derivation { name = "pkg-1.0"; system = "x86_64-linux"; builder = "/bin/sh"; }'
    ' // { meta.license = "mit"; }; in'
)


@pytest.fixture
def state(tmp_path: pathlib.Path) -> Any:
    from huggorm_bindings import EvalState, Store

    return EvalState(Store(str(tmp_path)))


@pytest.fixture
def recording(state: Any) -> Any:
    state.set_record_derivations(True)
    return state


def _out(state: Any) -> str:
    out: str = state.eval_expr(f"{PKG} pkg.outPath").string_value()
    return out


def test_nothing_is_recorded_unless_asked(state: Any) -> None:
    state.eval_expr(f'{PKG} "${{pkg}}"')
    assert state.recorded_out_paths() == []


def test_interpolation_records_the_value_with_its_meta(recording: Any) -> None:
    text = recording.eval_expr(f'{PKG} "uses ${{pkg}}"').string_value()
    out = _out(recording)
    assert text == f"uses {out}"
    assert recording.recorded_out_paths() == [out]
    meta = recording.recorded_derivation(out).get("meta")
    recording.force(meta)
    license = meta.get("license")
    recording.force(license)
    assert license.string_value() == "mit"


def test_to_json_records(recording: Any) -> None:
    recording.eval_expr(f"{PKG} builtins.toJSON {{ image = pkg; }}")
    assert recording.recorded_out_paths() == [_out(recording)]


def test_a_derivation_input_is_recorded(recording: Any) -> None:
    """`derivationStrict` coerces each attribute of the environment."""
    recording.eval_expr(
        f'{PKG} (derivation {{ name = "top"; system = "x86_64-linux"; builder = "/bin/sh"; dep = pkg; }}).drvPath'
    )
    assert _out(recording) in recording.recorded_out_paths()


def test_reading_out_path_is_no_coercion(recording: Any) -> None:
    _out(recording)
    assert recording.recorded_out_paths() == []


def test_an_attrset_that_is_no_derivation_is_not_recorded(recording: Any) -> None:
    assert recording.eval_expr('"${{ outPath = "/x"; }}"').string_value() == "/x"
    assert recording.recorded_out_paths() == []


def test_a_throwing_type_changes_no_result(recording: Any) -> None:
    """A coercion never forces `type`; the record must not either."""
    text = recording.eval_expr('"${{ outPath = "/x"; type = throw "no"; }}"').string_value()
    assert text == "/x"
    assert recording.recorded_out_paths() == []


def test_a_throwing_out_path_throws_the_same_error(tmp_path: pathlib.Path) -> None:
    """The record forces `outPath` first, and leaves its error to the coercion."""
    from huggorm_bindings import EvalState, Store
    from huggorm_bindings.errors import NixError

    def message(record: bool) -> str:
        state = EvalState(Store(str(tmp_path)))
        state.set_record_derivations(record)
        with pytest.raises(NixError) as caught:
            state.eval_expr('"${{ type = "derivation"; outPath = throw "no outPath"; }}"')
        return str(caught.value)

    plain = message(record=False)
    assert "no outPath" in plain
    assert message(record=True) == plain


def test_unknown_path_is_none(recording: Any) -> None:
    assert recording.recorded_derivation("/nix/store/missing") is None


def test_stopping_keeps_and_clearing_drops(recording: Any) -> None:
    recording.eval_expr(f'{PKG} "${{pkg}}"')
    recording.set_record_derivations(False)
    recording.eval_expr(f'{PKG} "${{pkg.out}} again"')
    assert recording.recorded_out_paths() == [_out(recording)]
    recording.clear_recorded_derivations()
    assert recording.recorded_out_paths() == []
