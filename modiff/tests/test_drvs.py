from __future__ import annotations

from modiff.model import DrvChange, Rebuild, package_name, pair_drvs, store_name

S = "/nix/store/"


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
