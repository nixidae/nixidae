# Give one project the inputs the umbrella owns.
#
# This is the seam every project enters through. Inside the umbrella,
# default.nix calls it. Outside, a project's own default.nix fetches the
# umbrella and calls it, so the umbrella decides the inputs either way and
# there is only one answer to keep right.
#
# nix/inputs.nix says what the values are.
{
  # The directory name of the project, which is also its name in
  # nix/inputs.nix.
  project,

  # Which copy of that project to wire.
  #
  # Null means the umbrella's own, which is what default.nix wants: the
  # submodule in this checkout.
  #
  # A project that pulled the umbrella in from outside passes its own
  # checkout instead. It then builds itself from the working copy the user is
  # sitting in, and takes its siblings from the umbrella that was fetched.
  # Without this it would build the submodule that came down with the
  # umbrella, and the user's edits would go nowhere.
  source ? null,

  inputs ? import ./inputs.nix,

  # One package set for each system, built from the nixpkgs above and handed
  # to every project as `self.legacyPackages`.
  #
  # The default is what nanopynix's own nix/compat.nix passed, and nixkube
  # asks for the same thing. Dropping it here would give a project that
  # reaches `inputs.nixpkgs.legacyPackages` a set with no allowUnfree, which
  # is not what it gets when it builds on its own.
  nixpkgsArgs ? (
    system: {
      inherit system;
      config.allowUnfree = true;
    }
  ),
}:
let
  wired = inputs // (if source == null then { } else { ${project} = source; });

  # nix/fetch.nix and not parseFlakeRef, because flake-compatish is a working
  # copy in this checkout now and parseFlakeRef takes a string.
  flake-compatish = import (import ./fetch.nix wired.flake-compatish);

  src = wired.${project};
in
# The overrides are the whole set. flake-compatish reads the project's own
# flake.nix for the names it declares and ignores the rest, so nothing here
# has to know which project wants what.
#
# `self` has to be the working copy too, or flake-compatish copies the
# project to the store before reading it, which is the round trip this
# arrangement exists to avoid.
(flake-compatish {
  source = src;
  overrides = wired // {
    self = src;
  };
  inherit nixpkgsArgs;
  warnOverrides = false;
}).inputs
