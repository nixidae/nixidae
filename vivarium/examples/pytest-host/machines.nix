# The guests examples/pytest-host's tests name. Each attribute is a
# `mkTest` with no phases of its own: `vivarium up` boots it, and pytest
# runs the tests (nixidae/nixidae#1413).
{
  sources ? import ../../nix/sources.nix,
  pkgs ? import sources.nixpkgs { },
}:
let
  vivarium = import ../.. { inherit sources pkgs; };
in
{
  one = vivarium.lib.mkTest {
    name = "one";
    backend = "qemu";
    nodes.vm = { };
  };
  pair = vivarium.lib.mkTest {
    name = "pair";
    backend = "qemu";
    nodes = {
      a.vivarium.lan = {
        network = "lan";
        address = "192.168.99.2/24";
      };
      b.vivarium.lan = {
        network = "lan";
        address = "192.168.99.3/24";
      };
    };
  };
}
