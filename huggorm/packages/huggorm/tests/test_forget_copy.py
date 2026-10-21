"""`forget_file` and the copy `"${./x}"` makes (nixidae/nixidae#940).

Nix keeps each path's copy in a per-state map, so after an edit the
interpolation names the old store path. 2.34 holds it on `EvalState`,
2.35 on `fetchers::Settings` (nixidae/nixidae#997).
"""

from __future__ import annotations

import pathlib

from huggorm_bindings import EvalState, Store


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
    # The negative control: the edit alone leaves the old copy.
    assert copy() == first
    state.forget_file(str(src))
    second = copy()
    assert second != first
    assert pathlib.Path(second).read_text() == "two\n"
