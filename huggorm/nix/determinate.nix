/**
  Determinate Nix, built against this nixpkgs: the component scope the
  fork's flake gives as `lib.makeComponents { pkgs }`, without reading the
  flake. Its other inputs come from flakehub.com, and only `bdwgc` reaches
  a build.

  nixpkgs' `nixComponents_*` builder cannot take this tree: it vendors its
  own `packaging/components.nix`, and the fork's takes other arguments and
  its own collector.

  Native only: no splicing, so no cross build.
*/
{ pkgs, sources }:
let
  inherit (pkgs) lib;
  src = sources.nix-determinate;
  dependencies = lib.makeScope pkgs.newScope (
    import "${src}/packaging/dependencies.nix" {
      inputs.bdwgc = sources.bdwgc-determinate;
      inherit pkgs;
      inherit (pkgs) stdenv;
    }
  );
in
{
  # Not meson's default build directory, `build`. With unity on, meson
  # 1.12 reads `src/libstore/build/build-log.cc` as a file in that
  # directory and includes `build/build/build-log.cc`. The fork pins
  # nixpkgs 26.05, whose meson 1.10 does not.
  components =
    (lib.makeScope dependencies.newScope (
      import "${src}/packaging/components.nix" {
        inherit lib pkgs src;
        officialRelease = true;
        maintainers = [ ];
      }
    )).overrideAllMesonComponents
      (_: _: { mesonBuildDir = "_build"; });
  # Every component states the fork's version (`.version-determinate`).
  # `.version` holds the NixOS/nix release it builds on.
  upstreamVersion = lib.fileContents "${src}/.version";
  # The fork's bdwgc, with the fork's configuration.
  inherit (dependencies) boehmgc;
}
