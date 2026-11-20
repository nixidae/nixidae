"""A module system small enough to evaluate in milliseconds, shaped to hit
each case the walk handles: a dependent option, an untouched one, shared
`true` values, a list, a derivation."""

from __future__ import annotations

from typing import Final

from modiff.engine import Source
from modiff.model import Path

TOY: Final = """
let
  lib = import <nixpkgs/lib>;
  # Outside every module, as `pkgs` is when it comes through
  # `specialArgs`. The edited evaluation shares `shared` and coerces it
  # again, but never instantiates it again, so it never records `deep`.
  shared = derivation {
    name = "shared";
    system = "x86_64-linux";
    builder = "/bin/sh";
    deep = derivation { name = "deep"; system = "x86_64-linux"; builder = "/bin/sh"; };
  };
  # Shared too, and no edit changes it or anything that reads it: only the
  # walk, which finds it unchanged, says the new side still reaches `still`.
  steady = derivation {
    name = "steady";
    system = "x86_64-linux";
    builder = "/bin/sh";
    still = derivation { name = "still"; system = "x86_64-linux"; builder = "/bin/sh"; };
  };
in lib.evalModules {
  modules = [
    # An edit to `old` reaches `a` and never `config.old`.
    (lib.mkRenamedOptionModule [ "old" ] [ "a" ])
    ({ lib, config, ... }: {
      options = {
        warnings = lib.mkOption { type = lib.types.listOf lib.types.str; default = [ ]; };
        a = lib.mkOption { type = lib.types.int; default = 1; description = "The input."; };
        b = lib.mkOption { type = lib.types.int; };
        c = lib.mkOption { type = lib.types.str; default = "untouched"; };
        # Sorts after `checks`, which reads it: the walk meets a conflict
        # on `z` first in `checks`, as NixOS meets one in `assertions`.
        z = lib.mkOption { type = lib.types.int; };
        # `tryEval` leaves `bad` a failed value in the base, not a thunk.
        tried = lib.mkOption { type = lib.types.attrs; };
        # One item the base never forced: Nix's printer forces it to choose
        # a layout.
        solo = lib.mkOption { type = lib.types.attrs; };
        flags = lib.mkOption { type = lib.types.attrs; };
        lines = lib.mkOption { type = lib.types.listOf lib.types.str; };
        drv = lib.mkOption { type = lib.types.package; };
        steady = lib.mkOption { type = lib.types.package; default = steady; };
        # The whole configuration again, as home-manager's `nixosConfig`.
        alias = lib.mkOption { type = lib.types.raw; default = { cfg = config; }; };
        # Each level holds the one below twice: 2^22 paths to the last. The
        # edited evaluation builds it again, so no value is shared.
        wide = lib.mkOption {
          type = lib.types.raw;
          default =
            let
              chain = builtins.foldl' (s: _: { a = s; b = s; }) { x = 1; } (builtins.genList (i: i) 22);
            in
            builtins.deepSeq chain chain;
        };
        # `wide` with a leaf that reads `seed`: an edit to `seed` changes
        # every level, so no pair compares clean.
        seed = lib.mkOption { type = lib.types.int; default = 0; };
        fan = lib.mkOption {
          type = lib.types.raw;
          default =
            let
              chain = builtins.foldl' (s: _: { a = s; b = s; }) { x = config.seed; } (builtins.genList (i: i) 22);
            in
            builtins.deepSeq chain chain;
        };
        # An edit to `[ ]` removes a derivation and adds none.
        extras = lib.mkOption {
          type = lib.types.listOf lib.types.package;
          default = [
            (derivation {
              name = "extra";
              system = "x86_64-linux";
              builder = "/bin/sh";
              src = derivation { name = "extra-src"; system = "x86_64-linux"; builder = "/bin/sh"; };
            })
          ];
        };
        # Shaped like `system.build.toplevel`: its inputs are coerced to
        # their output paths, which records them.
        top = lib.mkOption { type = lib.types.package; };
        checks = lib.mkOption { type = lib.types.listOf lib.types.attrs; };
        ports = lib.mkOption { type = lib.types.listOf lib.types.port; default = [ 22 ]; };
        # Longer than Nix's default print budget: a change at the end elides.
        many = lib.mkOption { type = lib.types.listOf lib.types.int; };
        # A submodule merges each field into a thunk: on the new side no
        # field is a value until something forces it.
        people = lib.mkOption {
          type = lib.types.listOf (lib.types.submodule {
            options.name = lib.mkOption { type = lib.types.str; };
            options.uid = lib.mkOption { type = lib.types.int; };
          });
        };
      };
      config = {
        # Shaped like NixOS `assertions`: the base forces `ok` and never
        # `msg`; `name` is a literal, so a value with no forcing.
        checks = lib.mkMerge [
          (lib.mkBefore [ { name = "first"; ok = config.a > 0 && config.z > 0; msg = "a is ${toString config.a}"; } ])
          [
            { name = "second"; ok = true; msg = "b is ${toString config.b}"; }
            { name = "third"; ok = true; msg = "c is ${config.c}"; }
            # A literal `msg` is a value with no forcing; the others stay thunks.
            {
              name = "fourth";
              ok = true;
              msg = "always";
              # Forced to its `drvPath`: a derivation the base keyed.
              tool = derivation { name = "tool"; system = "x86_64-linux"; builder = "/bin/sh"; };
            }
          ]
        ];
        many = lib.genList (i: i) 250;
        people = [
          { name = "alice"; uid = 1; }
          { name = "bob"; uid = 2; }
          { name = "carol"; uid = 3; }
        ];
        b = config.a * 10;
        z = 3;
        solo = { only = config.a * 2; };
        tried = { bad = throw "never works"; ok = (builtins.tryEval config.tried.bad).success; };
        flags = { p = true; q = true; };
        lines = [ "one" "two" ];
        drv = derivation {
          name = "toy-${toString config.b}";
          system = "x86_64-linux";
          builder = "/bin/sh";
        };
        top = derivation {
          name = "top";
          system = "x86_64-linux";
          builder = "/bin/sh";
          # Not `config.drv`: the tests need one derivation with no path read.
          inputs = config.extras ++ [
            shared
            (derivation {
              name = "dep-${toString config.b}";
              system = "x86_64-linux";
              builder = "/bin/sh";
            })
            # Two of one name, as NixOS has a system and a user
            # `unit-dbus-broker.service`. With these values the store
            # paths sort so that pairing in path order pairs them wrongly.
            (derivation {
              name = "unit";
              system = "x86_64-linux";
              builder = "/bin/sh";
              which = "system";
              b = toString config.b;
            })
            (derivation {
              name = "unit";
              system = "x86_64-linux";
              builder = "/bin/sh";
              which = "user-unit";
              b = toString config.b;
            })
          ];
          # A path read off a derivation coerces no derivation, so nothing
          # records `hidden`: an input with no row of its own.
          hidden = (derivation {
            name = "hidden-${toString config.b}";
            system = "x86_64-linux";
            builder = "/bin/sh";
          }).outPath;
        };
      };
    })
  ];
}
"""

FORCE: Final[tuple[Path, ...]] = (
    ("config", "b"),
    ("config", "drv", "name"),
    ("config", "top", "drvPath"),
    ("config", "steady", "drvPath"),
    ("config", "wide"),
    ("config", "fan"),
    ("config", "alias", "cfg", "b"),
    ("config", "flags", "p"),
    ("config", "flags", "q"),
    ("config", "lines", 0),
    ("config", "lines", 1),
    ("config", "checks", 0, "ok"),
    ("config", "checks", 1, "ok"),
    ("config", "checks", 2, "ok"),
    ("config", "checks", 3, "ok"),
    ("config", "checks", 3, "tool", "drvPath"),
    ("config", "many"),
    ("config", "tried", "ok"),
    ("config", "solo"),
    ("config", "people", 0, "name"),
    ("config", "people", 0, "uid"),
    ("config", "people", 1, "name"),
    ("config", "people", 1, "uid"),
    ("config", "people", 2, "name"),
    ("config", "people", 2, "uid"),
)

SOURCE: Final = Source(TOY, FORCE)
