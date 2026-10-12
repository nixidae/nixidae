# Where every dependency of this repository lives: the nixidae umbrella.
#
# Inside the umbrella that is the checkout two directories up. Outside it,
# the umbrella is fetched at UMBRELLA_REV, or at the revision in
# nix/umbrella.rev, and this working copy replaces the copy that came down
# with it. huggorm's nix/sources.nix gives the reasons and the measurements.
let
  root = toString ../.;
  # `../..` from a bare store path leaves the store root, and Nix refuses to
  # evaluate it. `builtins.tryEval` does not catch that.
  escapesStore = builtins.match "/nix/store/[^/]+" root != null;
  inUmbrella = !escapesStore && builtins.pathExists ../../nix/wire.nix;

  umbrellaRev = builtins.getEnv "UMBRELLA_REV";
  pinMatch =
    if builtins.pathExists ./umbrella.rev then
      builtins.match "[ \n\t]*([0-9a-f]{40})[ \n\t]*" (builtins.readFile ./umbrella.rev)
    else
      null;
  pinned = if pinMatch == null then "" else builtins.head pinMatch;
  rev = if umbrellaRev != "" then umbrellaRev else pinned;

  wire =
    if inUmbrella then
      ../../nix/wire.nix
    else if rev != "" then
      (builtins.fetchTree (
        builtins.parseFlakeRef "git+https://github.com/nixidae/nixidae?rev=${rev}&shallow=1"
      )).outPath
      + "/nix/wire.nix"
    else
      throw "nix/sources.nix: set UMBRELLA_REV, or write an umbrella revision into nix/umbrella.rev.";
in
import wire { overrides.pydd = ../.; }
