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
        checks = lib.mkOption { type = lib.types.listOf lib.types.attrs; };
        ports = lib.mkOption { type = lib.types.listOf lib.types.port; default = [ 22 ]; };
      };
      config = {
        # Shaped like NixOS `assertions`: the base forces `ok` and never
        # `msg`; `name` is a literal, so a value with no forcing.
        checks = lib.mkMerge [
          (lib.mkBefore [ { name = "first"; ok = config.a > 0; msg = "a is ${toString config.a}"; } ])
          [
            { name = "second"; ok = true; msg = "b is ${toString config.b}"; }
            { name = "third"; ok = true; msg = "c is ${config.c}"; }
          ]
        ];
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
    ("config", "checks", 0, "ok"),
    ("config", "checks", 1, "ok"),
    ("config", "checks", 2, "ok"),
)

SOURCE: Final = Source(TOY, FORCE)
