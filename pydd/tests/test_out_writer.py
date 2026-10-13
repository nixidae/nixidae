"""`OutWriter`: the root's ATerm to `$out`, and a root that names a missing input refused."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pydd import Derivation, OutWriter

if TYPE_CHECKING:
    from pathlib import Path

BASH = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bash"
LEAF = "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-leaf.drv"


@pytest.fixture
def out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("out", str(tmp_path / "out"))
    monkeypatch.setenv("NIX_CONFIG", "extra-experimental-features = nix-command ca-derivations")
    return tmp_path / "out"


def writer() -> OutWriter:
    """A writer for a sandbox that holds `bash` and nothing else."""
    return OutWriter(present={BASH}.__contains__)


def root(drvs: dict[str, list[str]]) -> Derivation:
    return Derivation(name="root", system="x86_64-linux", builder=f"{BASH}/bin/sh", srcs=[BASH], drvs=drvs)


def test_a_root_of_inputs_reaches_out_as_aterm(out: Path) -> None:
    staged = writer()
    staged.submit(staged.add(root({})), "out")
    aterm = out.read_text()
    assert aterm.startswith("Derive([")
    assert f'["{BASH}"]' in aterm


def test_a_root_that_names_a_missing_derivation_is_refused(out: Path) -> None:
    staged = writer()
    with pytest.raises(RuntimeError, match="which this build does not have"):
        staged.submit(staged.add(root({LEAF: ["out"]})), "out")
    assert not out.exists()
