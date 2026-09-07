# A flake is not how this collection is driven. It is here so that somebody
# can get the umbrella tool with one command, and for nothing else.
#
# `default.nix` is the whole definition, and `umbrella` the program is what
# this exposes. The four projects each carry their own flake, so build them
# from their own directory.
#
# A flake also sees only what git tracks, and the contents of a submodule are
# not that, so a build from a checkout has to ask for them by name:
#
#     nix build '.?submodules=1#umbrella'
{
  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
  outputs =
    inputs:
    let
      inherit (inputs.nixpkgs) lib;
      forEachSystem = lib.genAttrs lib.systems.flakeExposed;
    in
    {
      packages = forEachSystem (
        system:
        let
          pkgs = import inputs.nixpkgs { inherit system; };
          defaultNix = import ./. { inherit pkgs; };
        in
        {
          default = defaultNix.umbrella;
          inherit (defaultNix) umbrella;
        }
      );
      devShells = forEachSystem (
        system:
        let
          pkgs = import inputs.nixpkgs { inherit system; };
        in
        {
          default = (import ./. { inherit pkgs; }).shell;
        }
      );
    };
}
