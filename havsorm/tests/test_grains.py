"""The grains, through Salt's loader."""

from typing import Any

import salt.loader


def test_the_loader_finds_the_nix_grains(opts: dict[str, Any]) -> None:
    grains = salt.loader.grains(opts, force_refresh=True)
    nix = grains["nix"]
    assert nix["system"] == "x86_64-linux", nix
    assert nix["version"].split(".")[0] == "2", nix
    assert isinstance(nix["store"], bool) and isinstance(nix["daemon"], bool), nix


def test_nixos_grains_read_the_system_links(monkeypatch: Any, tmp_path: Any) -> None:
    from havsorm.grains import havsorm as grains

    system = tmp_path / "nixos-system"
    system.mkdir()
    (system / "nixos-version").write_text("26.05.1234\n")
    links = {"/run/current-system": str(system), "/run/booted-system": str(system)}
    monkeypatch.setattr(grains, "_target", lambda link: links.get(link))
    assert grains.nixos() == {
        "nixos": {"system": str(system), "booted": str(system), "profile": None, "version": "26.05.1234"}
    }

    links.clear()
    assert grains.nixos() == {}
