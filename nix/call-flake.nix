# Read a flake we do not own, with the umbrella's sources in place of its own
# lock file.
#
# Our own repositories are not flakes. They take the sources the umbrella
# gives them and import what they want. A third party's repository is often a
# flake, and sometimes its outputs are the only way in, so this reads one.
# It is the only place a flake is read.
#
# Takes the *name*, not a directory. nix/inputs.nix holds a path for a
# working copy and a flake reference string for a locked source, and
# flake-compatish takes either as a source. A directory would not do: a
# fetched one is a string carrying store-path context, and flake-compatish
# would hand that to parseFlakeRef, which a pure evaluation refuses.
#
# The overrides are the whole set. flake-compatish reads the flake's own
# flake.nix for the names it declares and ignores the rest, so nothing here
# has to know what the flake wants. A name it does declare comes from the
# umbrella; a name it declares that the umbrella does not name comes from its
# own lock.
{
  inputs ? import ./inputs.nix,
}:
name:
(import (import ./fetch.nix inputs.flake-compatish) {
  source = inputs.${name};
  overrides = inputs;
  warnOverrides = false;
}).outputs
