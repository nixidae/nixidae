# examples/k8s-devenv: an app on a one-node cluster, reached from the
# host's shell by its Service name over HTTPS.
{
  pkgs,
  lib,
  hostPkgs,
  mkTest,
  ...
}:
import ../../examples/k8s-devenv {
  inherit
    mkTest
    lib
    pkgs
    hostPkgs
    ;
}
