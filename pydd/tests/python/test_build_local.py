"""`scripts/build-local.py`: the redirect of an editable install."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[2] / "src/pydd/python/scripts/build-local.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_local", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FINDER = """\
MAPPING: dict[str, str] = {'demo': '/build/project/src/demo'}
NAMESPACES: dict[str, list[str]] = {}
PATH_PLACEHOLDER = '__editable__.demo-0.1.0.finder' + ".__path_hook__"
"""


def test_a_setuptools_finder_reads_the_root(monkeypatch: pytest.MonkeyPatch) -> None:
    text, patched = load().patch_finder(FINDER, "/build/project", "$DEMO_ROOT")
    assert patched
    assert "/build/project" not in text
    monkeypatch.setenv("DEMO_ROOT", "/home/me/demo")
    namespace: dict[str, object] = {}
    exec(compile(text, "finder", "exec"), namespace)
    assert namespace["MAPPING"] == {"demo": "/home/me/demo/src/demo"}
    assert namespace["PATH_PLACEHOLDER"] == "__editable__.demo-0.1.0.finder.__path_hook__"


def test_a_finder_that_names_nothing_is_left_alone() -> None:
    text, patched = load().patch_finder(FINDER, "/elsewhere", "$DEMO_ROOT")
    assert not patched
    assert text == FINDER
