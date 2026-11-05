"""A free-threaded build keeps the GIL off.

CPython turns the GIL back on, with a warning and nothing else, when it
imports an extension that does not declare `Py_mod_gil`. The suite then
passes with the GIL on, and proves nothing about free-threading. Two
modules did so: every huggorm binding before `NB_FREE_THREADED`, and
`tree_sitter_nix._binding` (the `fod` extra).

Each check runs in a fresh interpreter, without `PYTHON_GIL`: the
free-threaded suite sets `PYTHON_GIL=0`, which forces the answer.
"""

from __future__ import annotations

import os
import subprocess
import sys
import sysconfig

import pytest

pytestmark = pytest.mark.skipif(
    not sysconfig.get_config_var("Py_GIL_DISABLED"), reason="a GIL build has no GIL to keep off"
)

_IMPORT_ALL = """
import importlib, pkgutil, sys
import huggorm_bindings, huggorm_generated
for package in (huggorm_bindings, huggorm_generated):
    for module in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        importlib.import_module(module.name)
import huggorm, huggorm.aio, huggorm.ssh
print(sys._is_gil_enabled())
"""


def _gil_after(code: str, *flags: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHON_GIL"}
    return subprocess.run([sys.executable, *flags, "-c", code], env=env, capture_output=True, text=True, check=False)


def test_importing_every_module_keeps_the_gil_off() -> None:
    """Every module but `huggorm.fod`, and with the warning an error, so
    the module that turned it on is named."""
    done = _gil_after(_IMPORT_ALL, "-W", "error::RuntimeWarning")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "False", done.stderr


@pytest.mark.skipif(os.environ.get("HUGGORM_SANITIZER") != "thread", reason="not a TSAN lane")
def test_a_tsan_lane_runs_on_an_instrumented_cpython() -> None:
    """Without the GIL, TSAN sees CPython's own locks only in a CPython
    built for it, and the uninstrumented one reports races inside itself.
    The lane builds on the plain interpreter and swaps this one in for
    the suite, so a swap that fell back would pass quietly."""
    assert "--with-thread-sanitizer" in str(sysconfig.get_config_var("CONFIG_ARGS"))


def test_fod_turns_the_gil_on_through_py_tree_sitter() -> None:
    """py-tree-sitter declares `Py_MOD_GIL_USED`: its parser and trees are
    not thread-safe (tree-sitter/py-tree-sitter#458). `tree_sitter_nix`
    declares that it needs no GIL, so py-tree-sitter is the only cause.
    The day this fails, `huggorm.fod` belongs in the test above."""
    done = _gil_after("import sys, tree_sitter_nix; print(sys._is_gil_enabled())", "-W", "error::RuntimeWarning")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "False", done.stderr
    done = _gil_after("import sys, huggorm.fod; print(sys._is_gil_enabled())")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "True"
    assert "'tree_sitter._binding'" in done.stderr
