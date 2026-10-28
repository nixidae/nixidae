"""A warm evaluator over files on disk, as the ext_pillar reads them."""

import pathlib
from typing import Any

from havsorm import nixeval


def _pillar(root: pathlib.Path) -> Any:
    return nixeval.evaluate(
        lambda ev: ev.state.eval_file(str(root)), {"id": "m"}, "dummy://", str(root), RuntimeError, keep=True
    )


def test_a_warm_evaluation_sees_an_edited_import_on_disk(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "default.nix"
    roles = tmp_path / "roles.nix"
    root.write_text("{ id, ... }: { role = (import ./roles.nix).${id}; }\n")
    roles.write_text('{ m = "web"; }\n')
    assert _pillar(root) == {"role": "web"}
    evaluator = nixeval.warm("dummy://")
    assert str(roles) in evaluator.state.cached_files()

    assert _pillar(root) == {"role": "web"}
    assert nixeval.warm("dummy://") is evaluator

    roles.write_text('{ m = "database"; }\n')
    assert _pillar(root) == {"role": "database"}

    root.write_text("{ id, ... }: { role = (import ./roles.nix).${id}; id = id; }\n")
    assert _pillar(root) == {"role": "database", "id": "m"}
