{
  inputs ? import ./nix/inputs.nix,
  system ? builtins.currentSystem,
  pkgs ? import (import ./nix/fetch.nix inputs.nixpkgs) {
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
  # nix/wire.nix does the work, and each project's own default.nix calls it
  # too. Passing `inputs` here rather than letting it read nix/inputs.nix
  # itself is what makes an argument to this file reach the projects.
  projectInputs = project: import ./nix/wire.nix { inherit project inputs; };

  nanopynix = import ./nanopynix {
    inputs = projectInputs "nanopynix";
    inherit system pkgs;
  };

  easykubenix = import ./easykubenix {
    inputs = projectInputs "easykubenix";
    inherit system pkgs;
  };

  # No `inputs` argument, and it needs none. nixpkgs is the only input it has
  # that reaches a build, and `pkgs` is that already resolved.
  pynixd = import ./pynixd { inherit pkgs; };

  # It builds its own package set, because it applies an overlay of its own,
  # so it takes the sources rather than the set.
  nixkube = import ./nixkube {
    inputs = projectInputs "nixkube";
    inherit system;
  };

  checks = {
    # That the wiring above is real. nix/wired.nix says why an override that
    # quietly does not apply is worse than one that breaks.
    wired = pkgs.callPackage ./nix/wired.nix { inherit projectInputs inputs; };
  };

  # -- what the above is built out of --------------------------------------

  # umbrella is a source like any other now, so nix/sources.nix says where it
  # comes from and nix/sources.lock says which revision. The fall-back for a
  # clone made without the submodules is the same rule every other name gets,
  # rather than a revision written down here.
  umbrellaSource = import ./nix/fetch.nix inputs.umbrella;
}
