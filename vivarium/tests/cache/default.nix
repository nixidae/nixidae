/*
  Can an unprivileged, read-only reader open the guest's Nix
  database, as on a running host? harmonia's cache is one.
*/
# `vivarium.recipes.cache`: a guest fetches what only the cache guest
# holds, and a path nobody holds fails at once.
{ pkgs, mkTest, ... }:
mkTest {
  name = "cache";
  vivarium.recipes.cache = {
    enable = true;
    roots = [ "${pkgs.hello}" ];
  };
  nodes.client = { };
  # In no guest's `lateClosure`: `client` must fetch it from the cache.
  settings.hello = "${pkgs.hello}";
  phases.cache = {
    script = ../phases/cache.py;
    after = [ "boot" ];
  };
}
