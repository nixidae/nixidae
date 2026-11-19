"""A module system small enough to evaluate in milliseconds, shaped to hit
each case the walk handles: a dependent option, an untouched one, shared
`true` values, a list, a derivation."""

from __future__ import annotations

from typing import Final

from modiff.engine import Source
from modiff.model import Path

TOY: Final = """
let
  lib = import /etc/nixpkgs/lib;
in lib.evalModules {
  modules = [
    ({ lib, config, ... }: {
      options = {
        a = lib.mkOption { type = lib.types.int; default = 1; description = "The input."; };
        b = lib.mkOption { type = lib.types.int; };
        c = lib.mkOption { type = lib.types.str; default = "untouched"; };
        flags = lib.mkOption { type = lib.types.attrs; };
        lines = lib.mkOption { type = lib.types.listOf lib.types.str; };
        drv = lib.mkOption { type = lib.types.package; };
      };
      config = {
        b = config.a * 10;
        flags = { p = true; q = true; };
        lines = [ "one" "two" ];
        drv = derivation {
          name = "toy-${toString config.b}";
          system = "x86_64-linux";
          builder = "/bin/sh";
        };
      };
    })
  ];
}
"""

FORCE: Final[tuple[Path, ...]] = (
    ("config", "b"),
    ("config", "drv", "name"),
    ("config", "flags", "p"),
    ("config", "flags", "q"),
    ("config", "lines", 0),
    ("config", "lines", 1),
)

SOURCE: Final = Source(TOY, FORCE)
