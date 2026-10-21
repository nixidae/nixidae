"""`forget_file` and the copy `"${./x}"` makes (nixidae/nixidae#940).

Nix 2.34 keeps each path's copy in a private per-state map
(`EvalState::srcToStore`), so after an edit the interpolation still
named the old store path. 2.35 has no such map and copies again.
"""

from __future__ import annotations

import pathlib

from huggorm_bindings import EvalState, Store
from huggorm_dsl.declare import NIX_2_35


def _store(tmp_path: pathlib.Path) -> Store:
    """A store whose store directory is a real path, so a copy works in a
    build sandbox."""
    return Store(f"local?store={tmp_path}/nix/store&state={tmp_path}/nix/var/nix"
                 f"&log={tmp_path}/nix/var/log/nix")


def test_forgetting_a_path_copies_it_again(tmp_path: pathlib.Path) -> None:
    src = tmp_path / "motd"
    src.write_text("one\n")
    state = EvalState(_store(tmp_path))

    def copy() -> str:
        return state.eval_expr(f'"${{{src}}}"', str(tmp_path)).string_value()

    first = copy()
    src.write_text("two\n")
    # The negative control: on 2.34 the edit alone leaves the old copy.
    if not NIX_2_35:
        assert copy() == first
    state.forget_file(str(src))
    second = copy()
    assert second != first
    assert pathlib.Path(second).read_text() == "two\n"
