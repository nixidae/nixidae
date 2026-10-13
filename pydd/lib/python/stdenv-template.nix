# stdenv as data: what `stdenv.mkDerivation` hands to `derivation`, for a
# node that `graph.nix` writes with no nixpkgs in the planner.
#
# The host evaluates `mkDerivation` once. A string keeps its context as
# `{ text, __context }`, which the planner's decoders put back, so a node
# names stdenv and each hook as outputs of their derivations, as
# `mkDerivation` would.
{ pkgs }:
let
  # `nativeBuildInputs` is the hooks: `graph.nix` puts a node's tools before
  # them, as the order of `mkDerivation`'s list in `mkSource`.
  template = pkgs.stdenv.mkDerivation {
    name = "template";
    nativeBuildInputs = [ pkgs.autoPatchelfHook ];
  };
in
rec {
  # With context, for the host: each path of it is an input of a planner.
  raw = removeAttrs template.drvAttrs [ "name" ];
  # As JSON carries it, for the planner.
  encoded = import ./carry.nix raw;
}
