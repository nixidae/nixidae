from __future__ import annotations

import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from vivarium.pytest_host import (
    Measure,
    Want,
    batch_files,
    batch_key,
    batches,
    ends_after,
    out_name,
    project_filter,
    run_key,
    scope_key,
    store_root,
)

if TYPE_CHECKING:
    from pytest import Pytester

pytest_plugins = ["pytester"]

SOURCE = """
import pytest

@pytest.mark.vivarium("pair")
class TestPair:
    def test_one(self): pass
    def test_two(self): pass

@pytest.mark.vivarium("one", scope="function")
def test_fresh_a(): pass

@pytest.mark.vivarium("one", scope="function")
def test_fresh_b(): pass

@pytest.mark.vivarium("one", scope="module")
def test_module_a(): pass

@pytest.mark.vivarium("one", scope="module")
def test_module_b(): pass

def test_plain(): pass
"""


@pytest.fixture
def items(pytester: Pytester) -> dict[str, pytest.Item]:
    return {item.name: item for item in pytester.getitems(SOURCE)}


def test_a_class_shares_one_run(items: dict[str, pytest.Item]) -> None:
    assert run_key(items["test_one"]) == run_key(items["test_two"])
    assert not ends_after(items["test_one"], items["test_two"])
    assert ends_after(items["test_two"], items["test_fresh_a"])


def test_a_function_scope_ends_after_each_test(items: dict[str, pytest.Item]) -> None:
    assert run_key(items["test_fresh_a"]) != run_key(items["test_fresh_b"])
    assert ends_after(items["test_fresh_a"], items["test_fresh_b"])


def test_a_module_scope_spans_its_tests(items: dict[str, pytest.Item]) -> None:
    assert run_key(items["test_module_a"]) == run_key(items["test_module_b"])
    assert not ends_after(items["test_module_a"], items["test_module_b"])
    assert ends_after(items["test_module_b"], None)


def test_an_unmarked_test_has_no_run(items: dict[str, pytest.Item]) -> None:
    assert run_key(items["test_plain"]) is None
    assert not ends_after(items["test_plain"], None)


def test_a_class_scope_outside_a_class_is_its_module(items: dict[str, pytest.Item]) -> None:
    module = items["test_plain"].getparent(pytest.Module)
    assert module is not None
    assert scope_key(items["test_fresh_a"], "class") == module.nodeid
    assert scope_key(items["test_fresh_a"], "session") == ""


def test_a_bad_scope_is_a_usage_error(pytester: Pytester) -> None:
    [item] = pytester.getitems('import pytest\n@pytest.mark.vivarium("x", scope="day")\ndef test_x(): pass\n')
    with pytest.raises(pytest.UsageError, match="scope is one of"):
        Want.of(item)


def test_an_attribute_path_is_a_directory_name() -> None:
    assert out_name("k8s.qemu") == "k8s.qemu"
    assert out_name("a/b c") == "a-b-c"


GUESTS = """
import pytest

pytestmark = pytest.mark.vivarium("one", guest="vm", scope="module")

def test_a(): pass
def test_b(): pass

@pytest.mark.vivarium("one", guest="vm", scope="class")
class TestOwn:
    def test_c(self): pass
    def test_d(self): pass

@pytest.mark.vivarium("one")
def test_drives(): pass
"""


def test_a_scope_in_a_guest_is_one_batch(pytester: Pytester) -> None:
    items = pytester.getitems(GUESTS)
    grouped = batches(items)
    names = sorted([nodeid.split("::")[-1] for nodeid in group] for group in grouped.values())
    assert names == [["test_a", "test_b"], ["test_c", "test_d"]]
    assert batch_key(next(item for item in items if item.name == "test_drives")) is None


def test_a_guest_name_must_be_a_string(pytester: Pytester) -> None:
    [item] = pytester.getitems('import pytest\n@pytest.mark.vivarium("x", guest=3)\ndef test_x(): pass\n')
    with pytest.raises(pytest.UsageError, match="guest's name"):
        Want.of(item)


def test_the_guest_gets_each_file_once_from_any_directory(tmp_path: Path) -> None:
    nodeids = ["tests/test_x.py::TestA::test_b[1]", "test_y.py", "tests/test_x.py::test_c"]
    assert batch_files(tmp_path, nodeids) == [f"{tmp_path}/tests/test_x.py", f"{tmp_path}/test_y.py"]


def test_the_guest_measures_what_the_host_measures(tmp_path: Path) -> None:
    measure = Measure(tmp_path / ".coverage", ("pkg", "other"), True, ".coveragerc")
    assert measure.worker_args("/artifacts/pytest/b.coverage") == [
        "--coverage",
        "/artifacts/pytest/b.coverage",
        "--cov-config",
        ".coveragerc",
        "--cov-branch",
        "--cov-source",
        "pkg",
        "--cov-source",
        "other",
    ]
    # A bare --cov measures everything: no source at all.
    assert "--cov-source" not in Measure(tmp_path / ".coverage", None, False, ".coveragerc").worker_args("f")


def test_the_python_must_come_from_the_store() -> None:
    assert store_root("/nix/store/abc-env/bin/python3") == "/nix/store/abc-env"
    with pytest.raises(pytest.UsageError, match="Nix store"):
        store_root("/usr/bin/python3")


@pytest.mark.parametrize(
    ("name", "kept"),
    [
        ("./tests/test_x.py", True),
        ("./.vivarium", False),
        ("./.git", False),
        ("./pkg/__pycache__", False),
        ("./result-bin", False),
    ],
)
def test_the_project_tarball_leaves_out_runs_and_caches(name: str, kept: bool) -> None:
    assert (project_filter(tarfile.TarInfo(name)) is not None) is kept
