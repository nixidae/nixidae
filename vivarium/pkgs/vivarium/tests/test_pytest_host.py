from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from vivarium.pytest_host import Want, ends_after, out_name, run_key, scope_key

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
