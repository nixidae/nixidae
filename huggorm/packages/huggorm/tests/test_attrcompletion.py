"""The two completion rules of `SourceExprCommand::completeInstallable`.

pynix's `test_nix_equivalence.py` measures the answer against a real
`nix`; these state each rule on its own, so a failure names the rule.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from huggorm.attrcompletion import complete_file_attr_path, complete_flake_fragment
from huggorm.attrpath import AttrPathSearch
from huggorm.session import AsyncSession

URI = "dummy://"

#: `SourceExprCommand`'s pair, which `nix build` and `nix eval` use.
BASE = AttrPathSearch(
    prefixes=("packages.x86_64-linux.", "legacyPackages.x86_64-linux."),
    defaults=("packages.x86_64-linux.default", "defaultPackage.x86_64-linux"),
)

#: `nix develop`'s pair, from `develop.cc`.
DEV_SHELL = AttrPathSearch(
    prefixes=("devShells.x86_64-linux.", *BASE.prefixes),
    defaults=("devShells.x86_64-linux.default", "devShell.x86_64-linux", *BASE.defaults),
)

#: `pkgone` is under two roots: Nix's `std::set` lists it once.
FLAKE = """{
  packages.x86_64-linux = { pkgone = "one"; pkgtwo = "two"; default = "def"; };
  legacyPackages.x86_64-linux = { legone = "leg"; pkgone = "shadowed"; };
  devShells.x86_64-linux.shellone = "sh";
  topone = "top";
  toptwo.deep = "d";
}"""

#: `"a.b"` holds a dot, which only quotation marks reach.
FILE = """{
  nixos.config.system = "s";
  nixosLater.marker = "m";
  "a.b".inner = "i";
}"""


@pytest.fixture
async def state() -> AsyncIterator[Any]:
    async with AsyncSession(URI) as session:
        yield session.eval(session.store())


@pytest.mark.parametrize(
    ("prefix", "candidates"),
    [
        ("", ["a.b", "nixos", "nixosLater"]),
        ("nixo", ["nixos", "nixosLater"]),
        ("nixosL", ["nixosLater"]),
        ("nixos.", ["nixos.config"]),
        ("nixos.config.", ["nixos.config.system"]),
        ("nixos.absent.", []),
        ("nixos.config.system.", []),
    ],
)
async def test_the_file_branch_offers_the_whole_path(state: Any, prefix: str, candidates: list[str]) -> None:
    root = await state.eval_expr(FILE)
    assert await complete_file_attr_path(state, root, prefix) == candidates


async def test_the_file_branch_keeps_the_quotation_marks_the_caller_typed(state: Any) -> None:
    root = await state.eval_expr(FILE)
    assert await complete_file_attr_path(state, root, '"a.b".') == ['"a.b".inner']


async def test_an_empty_fragment_offers_the_union_of_every_root(state: Any) -> None:
    outputs = await state.eval_expr(FLAKE)
    assert await complete_flake_fragment(state, outputs, BASE, "") == [
        "",
        "default",
        "devShells",
        "legacyPackages",
        "legone",
        "packages",
        "pkgone",
        "pkgtwo",
        "topone",
        "toptwo",
    ]


async def test_a_name_that_two_roots_hold_appears_once(state: Any) -> None:
    outputs = await state.eval_expr(FLAKE)
    assert await complete_flake_fragment(state, outputs, BASE, "pkgone") == ["pkgone"]


async def test_the_search_of_the_command_decides_what_is_offered(state: Any) -> None:
    outputs = await state.eval_expr(FLAKE)
    assert await complete_flake_fragment(state, outputs, BASE, "shell") == []
    assert await complete_flake_fragment(state, outputs, DEV_SHELL, "shell") == ["shellone"]


async def test_a_leading_dot_clears_the_prefixes_and_stays_in_each_candidate(
    state: Any,
) -> None:
    outputs = await state.eval_expr(FLAKE)
    assert await complete_flake_fragment(state, outputs, BASE, ".top") == [".topone", ".toptwo"]


async def test_an_empty_fragment_offers_itself_when_a_default_resolves(state: Any) -> None:
    outputs = await state.eval_expr(FLAKE)
    assert "" in await complete_flake_fragment(state, outputs, BASE, "")
    without_default = await state.eval_expr('{ packages.x86_64-linux.pkgone = "one"; }')
    assert "" not in await complete_flake_fragment(state, without_default, BASE, "")


async def test_the_flake_branch_rebuilds_the_path_from_the_names(state: Any) -> None:
    outputs = await state.eval_expr(FLAKE)
    assert await complete_flake_fragment(state, outputs, BASE, "toptwo.") == ["toptwo.deep"]


async def test_the_empty_search_reads_the_fragment_as_one_path(state: Any) -> None:
    outputs = await state.eval_expr(FLAKE)
    assert await complete_flake_fragment(state, outputs, AttrPathSearch(), "") == [
        "devShells",
        "legacyPackages",
        "packages",
        "topone",
        "toptwo",
    ]


async def test_a_throwing_leaf_does_not_stop_its_set_completing(state: Any) -> None:
    root = await state.eval_expr('{ a = { bad = throw "no"; good = 1; }; }')
    assert await complete_file_attr_path(state, root, "a.") == ["a.bad", "a.good"]
