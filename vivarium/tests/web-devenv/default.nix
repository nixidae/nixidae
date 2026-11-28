# examples/web-devenv: plain NixOS guests by name and over HTTPS from the
# host's shell, through `vivarium.recipes.lab`.
{
  pkgs,
  lib,
  hostPkgs,
  mkTest,
  ...
}:
import ../../examples/web-devenv {
  inherit
    mkTest
    lib
    pkgs
    hostPkgs
    ;
}
