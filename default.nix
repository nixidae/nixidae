{
  inputs ? import ./nix/inputs.nix,
  system ? builtins.currentSystem,
  pkgs ? import inputs.nixpkgs {
    inherit system;
    # nixkube asks for this, and nanopynix asks for it through the
    # `nixpkgsArgs` of its own nix/compat.nix. One package set for everybody
    # means the umbrella has to ask for it too, or those two would each need
    # a set of their own and the sharing would be gone.
    config.allowUnfree = true;
  },
}:
rec {
  # umbrella drives this collection. It keeps a submodule commit that no
  # remote has out of the pointers recorded here, and it makes worktreespaces
  # that share storage instead of cloning every repository again.
  #
  # It is a submodule too, so it can be edited in place like the rest. It is
  # also the tool that checks the submodules out, so a clone made without them
  # has to be able to build it anyway: when the directory is not there, fall
  # back to the commit this repository pins.
  umbrella = (import umbrellaSource { inherit pkgs; }).umbrella;

  shell = pkgs.callPackage ./pkgs/shell { inherit umbrella; };

  # -- the four projects ---------------------------------------------------
  #
  # Each one keeps its own flake.nix and its own flake.lock, because each one
  # is still a repository somebody can build on its own. What changes here is
  # only which sources those declarations resolve to.
  #
  # `wire` reads a project's flake.nix for the list of inputs it declares,
  # then answers each of them from nix/inputs.nix instead of from its lock.
  # The important substitution is a sibling: easykubenix declares nanopynix
  # and gets ./nanopynix, so a change made in one is built by the other with
  # nothing published in between.
  #
  # `overrides` is the whole of nix/inputs.nix every time. flake-compatish
  # ignores a name the project does not declare, so no list of which project
  # wants what has to be written down here, or kept in step when one changes.
  #
  # `self` has to be the working copy too. Without it flake-compatish copies
  # the project to the store before reading it, which is the store round trip
  # this whole arrangement exists to avoid.
  projectInputs =
    source:
    (flake-compatish {
      inherit source;
      overrides = inputs // {
        self = source;
      };
      # Ten deliberate overrides in four projects is forty lines of warning
      # about the thing we asked for.
      warnOverrides = false;
    }).inputs;

  nanopynix = import ./nanopynix {
    inputs = projectInputs ./nanopynix;
    inherit system pkgs;
  };

  easykubenix = import ./easykubenix {
    inputs = projectInputs ./easykubenix;
    inherit system pkgs;
  };

  # No `inputs` argument, and it needs none. nixpkgs is the only input it has
  # that reaches a build, and `pkgs` is that already resolved.
  pynixd = import ./pynixd { inherit pkgs; };

  # It builds its own package set, because it applies an overlay of its own,
  # so it takes the sources rather than the set.
  nixkube = import ./nixkube {
    inputs = projectInputs ./nixkube;
    inherit system;
  };

  # -- what the above is built out of --------------------------------------

  # Fetched unpinned, like every other third-party input here. It is the one
  # that has to arrive before anything can be read, so it cannot come through
  # the same mechanism as the rest.
  flake-compatish = import (builtins.fetchTree (builtins.parseFlakeRef inputs.flake-compatish));

  umbrellaSource =
    if builtins.pathExists ./umbrella/default.nix then
      ./umbrella
    else
      pkgs.fetchFromGitHub {
        owner = "Lillecarl";
        repo = "umbrella";
        rev = "2302d8d376a8ce415fe544416958ba24f05922f6";
        hash = "sha256-y/Yownj5+DRPWKo3fATxPxpacNsP0SvKwU0DH483OYE=";
      };
}
