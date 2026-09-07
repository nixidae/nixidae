# Read a flake we do not own.
#
# Our own repositories are not flakes. They take the sources the umbrella
# gives them and import what they want. A third party's repository is often
# a flake, and its outputs are the only way in, so this reads one -- with the
# umbrella's sources in place of whatever its own lock names.
#
# flake-compatish takes the unfetched set, because it tells a path from a
# flake reference itself and a path is what it reads without a store copy.
{
  inputs ? import ./inputs.nix,
}:
source:
(import (import ./fetch.nix inputs.flake-compatish) {
  inherit source;
  overrides = inputs;
  warnOverrides = false;
}).outputs
