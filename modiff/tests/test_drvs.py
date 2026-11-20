from __future__ import annotations

from modiff.model import DrvChange, Rebuild, fold_inputs, package_name, pair_drvs, store_name

S = "/nix/store/"


def test_fold_inputs_folds_what_a_removed_path_needs() -> None:
    """`nginx` needs `pcre` and `src`, `pcre` needs `src` too; `unit` is
    rebuilt and needs `src`, which still folds: it is removed as well."""
    nginx, pcre, src, tool = (S + f"a-{n}" for n in ("nginx", "pcre", "src", "tool"))
    unit = DrvChange(S + "a-unit", S + "b-unit")
    inputs: dict[str, list[str]] = {nginx: [pcre, src], pcre: [src], src: [], tool: []}
    changes = (DrvChange(nginx, None), DrvChange(pcre, None), DrvChange(src, None), DrvChange(tool, None), unit)
    folded = fold_inputs(changes, lambda p: inputs.get(p, []))
    assert folded == (
        DrvChange(nginx, None, only=(pcre, src)),
        DrvChange(tool, None),
        unit,
    )
    assert fold_inputs((DrvChange(None, nginx), DrvChange(None, pcre)), lambda p: inputs[p]) == (
        DrvChange(None, nginx, only=(pcre,)),
    )


def test_fold_inputs_lists_direct_inputs_first() -> None:
    top, b, c, z = (S + f"a-{n}" for n in "tbcz")
    inputs: dict[str, list[str]] = {top: [z, b], b: [c], c: [], z: []}
    changes = tuple(DrvChange(p, None) for p in (top, b, c, z))
    assert fold_inputs(changes, lambda p: inputs[p]) == (DrvChange(top, None, only=(b, z, c)),)


def test_store_name_drops_the_hash() -> None:
    assert store_name(S + "abc-openssh-9.8p1-man") == "openssh-9.8p1-man"
    assert store_name(S + "abc-openssh-9.8p1.drv") == "openssh-9.8p1"


def test_package_name_follows_parse_drv_name() -> None:
    assert package_name("openssh-9.8p1") == "openssh"
    assert package_name("nixos-system-nixos-25.11") == "nixos-system-nixos"
    assert package_name("etc") == "etc"


def test_pairs_by_name_then_by_package() -> None:
    removed = (S + "a-etc", S + "b-openssh-9.7p1", S + "c-gone-1")
    added = (S + "d-etc", S + "e-openssh-9.8p1", S + "f-new-2")
    got = pair_drvs(added, removed)
    assert got == (
        DrvChange(S + "a-etc", S + "d-etc"),
        DrvChange(S + "c-gone-1", None),
        DrvChange(None, S + "f-new-2"),
        DrvChange(S + "b-openssh-9.7p1", S + "e-openssh-9.8p1"),
    )
    assert [c.kind for c in got] == [Rebuild.REBUILT, Rebuild.REMOVED, Rebuild.ADDED, Rebuild.VERSION]


def test_same_name_wins_over_same_package() -> None:
    """`openssh-9.8p1` must pair with itself, not with the old `-man`."""
    removed = (S + "a-openssh-9.8p1-man", S + "b-openssh-9.8p1")
    added = (S + "c-openssh-9.8p1",)
    assert pair_drvs(added, removed) == (
        DrvChange(S + "b-openssh-9.8p1", S + "c-openssh-9.8p1"),
        DrvChange(S + "a-openssh-9.8p1-man", None),
    )


def test_paths_of_one_name_pair_most_alike_first() -> None:
    removed = (S + "a-unit", S + "b-unit")
    added = (S + "c-unit", S + "d-unit")
    alike = {(S + "a-unit", S + "d-unit"), (S + "b-unit", S + "c-unit")}
    got = pair_drvs(added, removed, lambda old, new: 5 if (old, new) in alike else 1)
    assert set(got) == {DrvChange(S + "a-unit", S + "d-unit"), DrvChange(S + "b-unit", S + "c-unit")}
    # Without a likeness, path order decides.
    assert set(pair_drvs(added, removed)) == {
        DrvChange(S + "a-unit", S + "c-unit"),
        DrvChange(S + "b-unit", S + "d-unit"),
    }
