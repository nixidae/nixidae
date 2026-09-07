# Calling a flake we do not own, with our own inputs in place of its lock.
#
# This is the arm of the design that our own repositories no longer use: none
# of them is a flake, so none of them is read this way. A third party often
# is one, and sometimes its outputs are the only way in. `nix/call-flake.nix`
# is how, and this file is both the worked example and the gate over it.
#
#     nix build --file . checks.callFlake && cat result
#
# It is impure, and that is the point rather than a limitation. `nix flake`
# would make the consumer write the override as a `follows` in a lock file of
# their own. This reads the flake with an attribute set instead, so the
# caller decides at the call site and needs no lock.
#
# Three flakes, and each shows something the next does not.
#
# The gate can go red, which is what makes it one.
# `FLAKE_COMPATISH_DISABLE_OVERRIDES=1` turns the overrides off, and all
# three then fall back to their own lock files -- measured, treefmt-nix
# arrives as 7f0478ddr51i3r708dpkljnvmzwc2fhn instead of the nixpkgs here.
{
  lib,
  runCommand,
  # The package set itself: `mkWrapper` below takes one, not a directory.
  pkgs,
  # From default.nix.
  sources,
  callFlake,
  inputs,
}:
let
  # What a flake's own lock file names for one of its root inputs.
  #
  # This is what the caller is replacing, so it is what the check compares
  # against. A `follows` entry is a list rather than a node name; none of the
  # three below has one, and this says so rather than guessing.
  ownLock =
    name: inputName:
    let
      lock = builtins.fromJSON (builtins.readFile (sources.${name} + "/flake.lock"));
      spec = lock.nodes.${lock.root}.inputs.${inputName};
    in
    if builtins.isList spec then
      throw "${name} declares ${inputName} as a follows, which this example does not read"
    else
      (builtins.fetchTree lock.nodes.${spec}.locked).outPath;

  # -- the three cases -----------------------------------------------------

  # 1. treefmt-nix. One input, `nixpkgs`, and the umbrella owns that name, so
  #    the override applies and the flake's own revision is not fetched for
  #    it. `lib.mkWrapper` is the output, and it is built below, so this is
  #    not only an assertion about a path.
  treefmt = callFlake "treefmt-nix";

  # 2. pyproject-nix. The same shape on a second flake, which is the point:
  #    nothing here is specific to treefmt-nix.
  pyproject = callFlake "pyproject-nix";

  # 3. tree-sitter-nix. It declares `nixpkgs`, which the umbrella owns, and
  #    `flake-utils`, which it does not. So one name comes from here and the
  #    other from the flake's own lock, in the same evaluation.
  #
  #    **That is the rule, and it is worth stating.** An override set does
  #    not have to be complete. A name the umbrella does not carry is not an
  #    error and is not left unresolved: the flake's lock answers for it.
  treeSitter = callFlake "tree-sitter-nix-numtide";

  rows = [
    {
      name = "treefmt-nix declares nixpkgs";
      have = treefmt.inputs.nixpkgs.outPath;
      want = toString sources.nixpkgs;
    }
    {
      name = "treefmt-nix would have used";
      have = ownLock "treefmt-nix" "nixpkgs";
      want = "anything but ${toString sources.nixpkgs}";
      ok = ownLock "treefmt-nix" "nixpkgs" != toString sources.nixpkgs;
    }
    {
      name = "pyproject-nix declares nixpkgs";
      have = pyproject.inputs.nixpkgs.outPath;
      want = toString sources.nixpkgs;
    }
    {
      name = "tree-sitter-nix declares nixpkgs";
      have = treeSitter.inputs.nixpkgs.outPath;
      want = toString sources.nixpkgs;
    }
    {
      name = "tree-sitter-nix declares flake-utils, which we do not own";
      have = treeSitter.inputs.flake-utils.outPath;
      want = ownLock "tree-sitter-nix-numtide" "flake-utils";
    }
  ];

  judged = map (row: row // { ok = row.ok or (row.have == row.want); }) rows;
  wrong = builtins.filter (row: !row.ok) judged;

  line = row: "${if row.ok then "ok " else "BAD"}  ${row.name}\n       ${row.have}";
  report = lib.concatMapStringsSep "\n" line judged;
in
if wrong != [ ] then
  throw ''
    calling a flake does not use the umbrella's inputs:

    ${lib.concatMapStringsSep "\n    " (row: "${row.name}: wanted ${row.want}, got ${row.have}") wrong}
  ''
else
  # An output of a called flake, actually built. `mkWrapper` takes a package
  # set and a treefmt configuration, and the wrapper it returns runs. So this
  # gate covers the whole way through: read the flake, replace its nixpkgs,
  # call an output, build the result.
  runCommand "nixidae-call-flake"
    {
      inherit report;
      passAsFile = [ "report" ];
      wrapper = treefmt.lib.mkWrapper pkgs {
        projectRootFile = "default.nix";
        programs.nixfmt.enable = true;
      };
    }
    ''
      "$wrapper/bin/treefmt" --version > /dev/null
      cp "$reportPath" "$out"
    ''
